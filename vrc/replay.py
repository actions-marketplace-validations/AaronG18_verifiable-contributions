"""Replay the tests attached to a change against both the base and the head tree.

The base tree is materialised with `git archive`, then ONLY the test paths from
the head tree are overlaid on it. The source change is deliberately not copied,
so the attached tests must fail on the base tree for the change to be verified.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .classification import classify_failure
from .config import CONFIG_NAME, Config, ConfigError
from .receipt import (
    EXCERPT_LIMIT,
    Receipt,
    RunRecord,
    derive_verdict,
    sha256_text,
)

# Substring match against the variable NAME. Anything that looks like it could
# carry a credential must not reach the untrusted test process. Keep this list
# in sync with the contract test in tests/test_release_contract.py.
SAFE_ENV_DENY: tuple[str, ...] = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
    "API_KEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "PRIVATE",
    "AUTH_CONFIG",
    "WEBHOOK",
    "PEM",
    "SESSION_KEY",
    "COOKIE",
    "SIGNING_KEY",
    "KUBECONFIG",
)

# Exact names that do not contain any of the substrings above. Do NOT add
# short substrings like "PAT" to SAFE_ENV_DENY: it would also strip PATH,
# COMPAT and PATTERN and break every test command.
SAFE_ENV_DENY_EXACT: frozenset[str] = frozenset({"GH_PAT", "GITHUB_PAT", "NPM_CONFIG_USERCONFIG"})


class ReplayError(RuntimeError):
    """Raised when the replay cannot be performed at all."""


@dataclass(frozen=True)
class ReplayOutcome:
    receipt: Receipt
    base_dir: Path
    head_dir: Path


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ReplayError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:400]}"
        )
    return proc.stdout


def _resolve(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def _extract_tree(repo: Path, sha: str, destination: Path) -> None:
    """Extract a commit into destination without touching the working tree."""
    archive = _git(repo, "archive", "--format=tar", sha)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                name = member.name
                if (
                    name.startswith("/")
                    or ".." in Path(name).parts
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                    or member.isfifo()
                ):
                    raise ReplayError(f"refusing unsafe archive member: {name!r}")
                if not (member.isfile() or member.isdir()):
                    continue
                target = destination / name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise ReplayError(f"could not read archive member: {name!r}")
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
    except tarfile.TarError as exc:
        raise ReplayError(f"could not read git archive for {sha}: {exc}") from exc


def _safe_relative(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError(f"unsafe path in policy: {path!r}")
    return candidate


def _is_glob(path: Path) -> bool:
    return any(char in path.as_posix() for char in "*?[")


def _iter_files(root: Path, relative: Path) -> list[Path]:
    if _is_glob(relative):
        # `Path.glob` treats "**" as "this directory and below", so a pattern
        # like "**/*_test.go" selects test files anywhere without ever pulling
        # in the source files sitting next to them.
        return sorted(
            p.relative_to(root)
            for p in root.glob(relative.as_posix())
            if p.is_file() and not p.is_symlink()
        )
    target = root / relative
    if target.is_file():
        return [relative]
    if not target.is_dir():
        return []
    return sorted(
        p.relative_to(root) for p in target.rglob("*") if p.is_file() and not p.is_symlink()
    )


def _overlay_test_paths(base: Path, head: Path, test_paths: tuple[str, ...]) -> dict[str, str]:
    """Copy the head's test files onto the base tree; return their digests."""
    digests: dict[str, str] = {}
    for raw in test_paths:
        relative = _safe_relative(raw)
        head_files = _iter_files(head, relative)
        if _is_glob(relative):
            # Remove only the files this pattern matches, never a whole tree.
            for rel in _iter_files(base, relative):
                (base / rel).unlink()
        else:
            base_target = base / relative
            if base_target.is_dir():
                shutil.rmtree(base_target)
            elif base_target.is_file() or base_target.is_symlink():
                base_target.unlink()
        for rel in head_files:
            source = head / rel
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            digests[rel.as_posix()] = hashlib.sha256(dest.read_bytes()).hexdigest()
    return digests


def _protected_touched(repo: Path, base: str, head: str, protected: tuple[str, ...]) -> bool:
    changed = _git(repo, "diff", "--name-only", f"{base}..{head}").decode("utf-8", "replace")
    for name in changed.splitlines():
        for prefix in protected:
            normalized = prefix.rstrip("/")
            if name == normalized or name.startswith(normalized + "/") or name == prefix:
                return True
    return False


def _env_for_run() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in SAFE_ENV_DENY_EXACT
        and not any(deny in key.upper() for deny in SAFE_ENV_DENY)
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["VRC_VERIFICATION_RUN"] = "1"
    return env


def _run(
    command: tuple[str, ...],
    cwd: Path,
    timeout: int,
    *,
    environment_markers: tuple[str, ...],
    assertion_markers: tuple[str, ...],
) -> RunRecord:
    start = time.monotonic()
    env = _env_for_run()
    # The extracted tree must win over anything installed in the runner. Without
    # this, an editable install of the head checkout would be importable while
    # replaying the base tree, silently making the base run pass.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([str(cwd), existing] if existing else [str(cwd)])
    env["VRC_TREE"] = str(cwd)
    try:
        proc = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            env=env,
            shell=False,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        exit_code, timed_out = proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        stdout = (
            (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else ""
        )
        stderr = (
            (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else ""
        )
        stderr += f"\n[verifier] command exceeded {timeout}s and was terminated"
        exit_code, timed_out = 124, True
    except OSError as exc:
        stdout, stderr, exit_code, timed_out = (
            "",
            f"[verifier] could not start command: {exc}",
            127,
            False,
        )
    kind, evidence = classify_failure(
        exit_code,
        stdout,
        stderr,
        timed_out=timed_out,
        environment_markers=environment_markers,
        assertion_markers=assertion_markers,
    )
    return RunRecord(
        exit_code=exit_code,
        stdout_sha256=sha256_text(stdout),
        stderr_sha256=sha256_text(stderr),
        stdout_excerpt=stdout[-EXCERPT_LIMIT:],
        stderr_excerpt=stderr[-EXCERPT_LIMIT:],
        duration_seconds=time.monotonic() - start,
        timed_out=timed_out,
        failure_kind=kind,
        failure_evidence=evidence,
    )


def replay(
    repo: Path,
    base_ref: str,
    head_ref: str,
    *,
    workdir: Path,
    repository: str | None = None,
) -> ReplayOutcome:
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise ReplayError(f"not a git repository: {repo}")
    base_sha = _resolve(repo, base_ref)
    head_sha = _resolve(repo, head_ref)
    if base_sha == head_sha:
        raise ReplayError("base and head resolve to the same commit; nothing to verify")

    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    base_dir, head_dir = workdir / "base", workdir / "head"
    _extract_tree(repo, base_sha, base_dir)
    _extract_tree(repo, head_sha, head_dir)

    policy_path = base_dir / CONFIG_NAME
    config = Config.load(policy_path.read_bytes()) if policy_path.is_file() else Config.default()
    policy_sha256 = (
        sha256_text(policy_path.read_text(encoding="utf-8"))
        if policy_path.is_file()
        else sha256_text("")
    )

    test_files = _overlay_test_paths(base_dir, head_dir, config.test_paths)
    if not test_files:
        raise ReplayError(
            f"no test files found under {list(config.test_paths)!r}; "
            "a verification receipt without attached tests is meaningless"
        )

    protected = _protected_touched(repo, base_sha, head_sha, config.protected_paths)
    run_kwargs = {
        "environment_markers": config.environment_markers,
        "assertion_markers": config.assertion_markers,
    }
    base_run = _run(config.command, base_dir, config.timeout_seconds, **run_kwargs)
    head_run = _run(config.command, head_dir, config.timeout_seconds, **run_kwargs)
    verdict, reason = derive_verdict(base_run, head_run, protected)

    if repository is None:
        remote = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        repository = remote.stdout.decode().strip() or repo.name

    environment = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "verifier_version": __version__,
    }
    if os.environ.get("GITHUB_RUN_ID"):
        environment["github_run_id"] = os.environ["GITHUB_RUN_ID"]
        environment["github_workflow_sha"] = os.environ.get("GITHUB_SHA", "")

    receipt = Receipt(
        repository=repository,
        base_sha=base_sha,
        head_sha=head_sha,
        command=config.command,
        test_paths=config.test_paths,
        test_files_sha256=test_files,
        policy_sha256=policy_sha256,
        environment=environment,
        base_run=base_run,
        head_run=head_run,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        verdict=verdict,
        reason=reason,
        protected_touched=protected,
        environment_markers=config.environment_markers,
        assertion_markers=config.assertion_markers,
    )
    return ReplayOutcome(receipt=receipt, base_dir=base_dir, head_dir=head_dir)


def replay_summary(outcome: ReplayOutcome) -> dict[str, Any]:
    r = outcome.receipt
    return {
        "verdict": r.verdict,
        "reason": r.reason,
        "base_sha": r.base_sha,
        "head_sha": r.head_sha,
        "base_exit_code": r.base_run.exit_code,
        "head_exit_code": r.head_run.exit_code,
        "test_files": len(r.test_files_sha256),
        "protected_touched": r.protected_touched,
    }
