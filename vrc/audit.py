"""Per-change-point test-sensitivity audit (mutation testing scoped to a diff).

A whole-tree VERIFIED answers "does SOME test fail when the entire change is
removed?". It cannot answer "which of the five changed lines are actually
observed by a test?" — four covered points are enough to make the whole suite
fail on base, hiding the fifth completely.

This module reverts ONE diff hunk at a time on the head tree and re-runs the
exact same test command. The result is a reproducible experiment, not an
inference about the specification:

* PINNED     - reverting this point makes a test fail with a recognised
               assertion. At least one test observes this change.
* UNPINNED   - reverting this point leaves the whole suite passing. No test
               in this suite, under this command, distinguishes the change.
* INCONCLUSIVE - the reverted run failed for a non-assertion reason (import
               error, timeout, unrecognised output). Fail closed: no claim.

WHAT THIS CANNOT DO, and never claims to: decide what the change *should* do,
or what assertion a missing test should contain. That requires a specification
which is not present in the code (undecidable in general). The report therefore
separates PROVEN measurements from SUGGESTIONS, and every suggestion is paired
with a command that verifies it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .classification import KIND_ASSERTION, KIND_NONE
from .config import CONFIG_NAME, Config
from .receipt import RunRecord, canonical, sha256_text
from .replay import (
    ReplayError,
    _extract_tree,
    _git,
    _resolve,
    _run,
)

STATUS_PINNED = "PINNED"
STATUS_UNPINNED = "UNPINNED"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
_STATUSES = (STATUS_PINNED, STATUS_UNPINNED, STATUS_INCONCLUSIVE)

AUDIT_SCHEMA_VERSION = 1

VERDICT_ALL_PINNED = "ALL_PINNED"
VERDICT_GAPS = "GAPS_FOUND"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_INCOMPLETE = "INCOMPLETE"

# A suite that exits 0 but collected nothing is a fake PASS. These are checked
# on the BASELINE run before any mutation result can be trusted.
NO_TESTS_SIGNATURES: tuple[str, ...] = (
    "collected 0 items",
    "no tests ran",
    "no tests collected",
    "no test files found",
    "no tests found",
    "found no collectors",
    "0 tests",
)

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_PASSED = re.compile(r"(\d+) passed")
_COLLECTED = re.compile(r"collected (\d+) items?")


class AuditError(ReplayError):
    """Raised when an audit cannot be performed honestly."""


@dataclass(frozen=True)
class ChangePoint:
    """One independently revertable diff hunk."""

    index: int
    path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    base_lines: tuple[str, ...]
    head_lines: tuple[str, ...]
    status: str
    exit_code: int
    failure_kind: str
    failure_evidence: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_excerpt: str
    stderr_excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "path": self.path,
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "base_lines": list(self.base_lines),
            "head_lines": list(self.head_lines),
            "status": self.status,
            "exit_code": self.exit_code,
            "failure_kind": self.failure_kind,
            "failure_evidence": self.failure_evidence,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangePoint:
        return cls(
            index=int(data["index"]),
            path=str(data["path"]),
            old_start=int(data["old_start"]),
            old_count=int(data["old_count"]),
            new_start=int(data["new_start"]),
            new_count=int(data["new_count"]),
            base_lines=tuple(data["base_lines"]),
            head_lines=tuple(data["head_lines"]),
            status=str(data["status"]),
            exit_code=int(data["exit_code"]),
            failure_kind=str(data["failure_kind"]),
            failure_evidence=str(data["failure_evidence"]),
            stdout_sha256=str(data["stdout_sha256"]),
            stderr_sha256=str(data["stderr_sha256"]),
            stdout_excerpt=str(data["stdout_excerpt"]),
            stderr_excerpt=str(data["stderr_excerpt"]),
        )


@dataclass(frozen=True)
class AuditReport:
    schema_version: int
    repository: str
    base_sha: str
    head_sha: str
    command: tuple[str, ...]
    test_paths: tuple[str, ...]
    baseline: RunRecord
    tests_passed: int | None
    points: tuple[ChangePoint, ...]
    unaudited_points: int
    notes: tuple[str, ...]
    generated_at: str
    verifier_version: str
    origin: str
    verdict: str
    digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "command": list(self.command),
            "test_paths": list(self.test_paths),
            "baseline": self.baseline.to_dict(),
            "tests_passed": self.tests_passed,
            "points": [p.to_dict() for p in self.points],
            "unaudited_points": self.unaudited_points,
            "notes": list(self.notes),
            "generated_at": self.generated_at,
            "verifier_version": self.verifier_version,
            "origin": self.origin,
            "verdict": self.verdict,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.payload()
        data["digest"] = self.digest
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditReport:
        baseline = data["baseline"]
        return cls(
            schema_version=int(data["schema_version"]),
            repository=str(data["repository"]),
            base_sha=str(data["base_sha"]),
            head_sha=str(data["head_sha"]),
            command=tuple(data["command"]),
            test_paths=tuple(data["test_paths"]),
            baseline=RunRecord.from_dict(baseline)
            if hasattr(RunRecord, "from_dict")
            else _run_record(baseline),
            tests_passed=data.get("tests_passed"),
            points=tuple(ChangePoint.from_dict(p) for p in data["points"]),
            unaudited_points=int(data["unaudited_points"]),
            notes=tuple(data["notes"]),
            generated_at=str(data["generated_at"]),
            verifier_version=str(data["verifier_version"]),
            origin=str(data["origin"]),
            verdict=str(data["verdict"]),
            digest=str(data.get("digest", "")),
        )


def _run_record(data: dict[str, Any]) -> RunRecord:
    return RunRecord(
        exit_code=int(data["exit_code"]),
        stdout_sha256=str(data["stdout_sha256"]),
        stderr_sha256=str(data["stderr_sha256"]),
        stdout_excerpt=str(data["stdout_excerpt"]),
        stderr_excerpt=str(data["stderr_excerpt"]),
        duration_seconds=float(data["duration_seconds"]),
        timed_out=bool(data["timed_out"]),
        failure_kind=str(data["failure_kind"]),
        failure_evidence=str(data["failure_evidence"]),
    )


def _parse_hunks(patch: str) -> list[dict[str, Any]]:
    """Split a --unified=0 patch into hunks, with per-file paths."""
    hunks: list[dict[str, Any]] = []
    path: str | None = None
    current: dict[str, Any] | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            # Flush the previous file's pending hunk BEFORE switching files,
            # otherwise every file but the last silently loses its final hunk.
            if current is not None:
                hunks.append(current)
                current = None
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3].removeprefix("b/")
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            continue
        header = _HUNK_HEADER.match(line)
        if header:
            if current is not None:
                hunks.append(current)
            old_start = int(header.group(1))
            old_count = int(header.group(2) or 1)
            new_start = int(header.group(3))
            new_count = int(header.group(4) or 1)
            current = {
                "path": path or "",
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "base_lines": [],
                "head_lines": [],
            }
            continue
        if current is None:
            continue
        if line.startswith("-"):
            current["base_lines"].append(line[1:])
        elif line.startswith("+"):
            current["head_lines"].append(line[1:])
        elif line.startswith("\\"):
            continue
    if current is not None:
        hunks.append(current)
    return [h for h in hunks if h["path"]]


def _is_test_or_protected(path: str, config: Config) -> tuple[bool, str | None]:
    for raw in config.test_paths:
        if _matches(path, raw):
            return True, f"test path {raw!r}"
    for raw in config.protected_paths:
        if _matches(path, raw):
            return True, f"protected path {raw!r}"
    return False, None


def _matches(path: str, raw: str) -> bool:
    if any(char in raw for char in "*?["):
        return PurePosixPath(path).match(raw)
    normalized = raw.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def _apply_revert(tree: Path, hunk: dict[str, Any]) -> None:
    """Replace exactly this hunk's head lines with its base lines."""
    target = tree / hunk["path"]
    if not target.is_file():
        raise AuditError(f"cannot revert hunk in missing file {hunk['path']!r}")
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    start = hunk["new_start"] - 1
    end = start + hunk["new_count"]
    replacement = [text if text.endswith("\n") else text + "\n" for text in hunk["base_lines"]]
    lines[start:end] = replacement
    target.write_text("".join(lines), encoding="utf-8")


def _classify(record: RunRecord) -> str:
    if record.exit_code == 0 and record.failure_kind == KIND_NONE:
        return STATUS_UNPINNED
    if record.exit_code != 0 and record.failure_kind == KIND_ASSERTION:
        return STATUS_PINNED
    return STATUS_INCONCLUSIVE


def _count_and_sanity(record: RunRecord) -> tuple[int | None, str | None]:
    blob = f"{record.stdout_excerpt}\n{record.stderr_excerpt}".lower()
    for signature in NO_TESTS_SIGNATURES:
        if signature in blob:
            return None, f"no tests: output contains {signature!r}"
    passed = _PASSED.search(blob)
    collected = _COLLECTED.search(blob)
    if collected and collected.group(1) == "0":
        return None, "no tests: pytest collected 0 items"
    if passed is not None:
        return int(passed.group(1)), None
    # Non-pytest runners report success differently. Do not invent a count.
    return None, None


def audit(
    repo: Path,
    base_ref: str,
    head_ref: str,
    *,
    workdir: Path,
    max_points: int = 20,
    repository: str | None = None,
    only_point: int | None = None,
) -> AuditReport:
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise AuditError(f"not a git repository: {repo}")
    if max_points < 1:
        raise AuditError("max_points must be at least 1")
    base_sha = _resolve(repo, base_ref)
    head_sha = _resolve(repo, head_ref)
    if base_sha == head_sha:
        raise AuditError("base and head resolve to the same commit; nothing to audit")

    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    head_dir = workdir / "head"
    _extract_tree(repo, head_sha, head_dir)
    base_dir = workdir / "base"
    _extract_tree(repo, base_sha, base_dir)

    policy_path = base_dir / CONFIG_NAME
    config = Config.load(policy_path.read_bytes()) if policy_path.is_file() else Config.default()
    if config.working_directory and not (head_dir / config.working_directory).is_dir():
        raise AuditError(f"working_directory {config.working_directory!r} missing in head tree")

    patch = _git(
        repo, "diff", "--unified=0", "--no-color", "--no-ext-diff", base_sha, head_sha
    ).decode("utf-8", "replace")
    parsed = _parse_hunks(patch)
    notes: list[str] = []
    candidates: list[dict[str, Any]] = []
    for hunk in parsed:
        path = hunk["path"]
        if _looks_binary(hunk):
            notes.append(f"skipped binary change in {path!r}")
            continue
        excluded, reason = _is_test_or_protected(path, config)
        if excluded:
            notes.append(f"skipped {path!r}: {reason}")
            continue
        candidates.append(hunk)

    run_kwargs = {
        "environment_markers": config.environment_markers,
        "assertion_markers": config.assertion_markers,
        "working_directory": config.working_directory,
    }

    # Baseline: the unmodified head must genuinely PASS with tests actually
    # running. Otherwise no mutation result has any meaning.
    baseline = _run(config.command, head_dir, config.timeout_seconds, **run_kwargs)
    if baseline.exit_code != 0:
        raise AuditError(
            f"baseline head suite did not pass (exit {baseline.exit_code}); "
            "an audit of a red suite proves nothing"
        )
    tests_passed, sanity_problem = _count_and_sanity(baseline)
    if sanity_problem:
        raise AuditError(f"baseline is unsound: {sanity_problem}")

    if only_point is not None:
        if only_point < 1 or only_point > len(candidates):
            raise AuditError(
                f"--point {only_point} is out of range; this diff has "
                f"{len(candidates)} auditable change point(s)"
            )
        selected = [candidates[only_point - 1]]
        unaudited = 0
        notes.append(
            f"single-point reproduction: only change point {only_point} was audited; "
            "the verdict says nothing about the other points"
        )
    else:
        unaudited = max(0, len(candidates) - max_points)
        selected = candidates[:max_points]
        if unaudited:
            notes.append(
                f"{unaudited} change point(s) left unaudited by --max-points={max_points}; "
                "the verdict is INCOMPLETE, not a clean bill of health"
            )

    points: list[ChangePoint] = []
    for index, hunk in ((only_point, selected[0]),) if only_point else enumerate(selected, 1):
        mutant = workdir / f"mutant-{index}"
        if mutant.exists():
            shutil.rmtree(mutant)
        shutil.copytree(head_dir, mutant)
        _apply_revert(mutant, hunk)
        record = _run(config.command, mutant, config.timeout_seconds, **run_kwargs)
        points.append(
            ChangePoint(
                index=index,
                path=hunk["path"],
                old_start=hunk["old_start"],
                old_count=hunk["old_count"],
                new_start=hunk["new_start"],
                new_count=hunk["new_count"],
                base_lines=tuple(hunk["base_lines"]),
                head_lines=tuple(hunk["head_lines"]),
                status=_classify(record),
                exit_code=record.exit_code,
                failure_kind=record.failure_kind,
                failure_evidence=record.failure_evidence,
                stdout_sha256=record.stdout_sha256,
                stderr_sha256=record.stderr_sha256,
                stdout_excerpt=record.stdout_excerpt,
                stderr_excerpt=record.stderr_excerpt,
            )
        )

    statuses = [p.status for p in points]
    if unaudited:
        verdict = VERDICT_INCOMPLETE
    elif not points:
        verdict = VERDICT_INCONCLUSIVE
        notes.append("no source change points found; nothing could be audited")
    elif STATUS_UNPINNED in statuses:
        verdict = VERDICT_GAPS
    elif STATUS_INCONCLUSIVE in statuses:
        verdict = VERDICT_INCONCLUSIVE
    else:
        verdict = VERDICT_ALL_PINNED

    if repository is None:
        repository = repo.name

    report = AuditReport(
        schema_version=AUDIT_SCHEMA_VERSION,
        repository=repository,
        base_sha=base_sha,
        head_sha=head_sha,
        command=config.command,
        test_paths=config.test_paths,
        baseline=baseline,
        tests_passed=tests_passed,
        points=tuple(points),
        unaudited_points=unaudited,
        notes=tuple(notes),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        verifier_version=__version__,
        origin="ci" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
        verdict=verdict,
        digest="",
    )
    digest = sha256_text(canonical(report.payload()))
    return replace(report, digest=digest)


def _looks_binary(hunk: dict[str, Any]) -> bool:
    return not hunk["base_lines"] and not hunk["head_lines"]


def check_audit(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """Recompute the digest and re-derive every status from the raw observations."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return False, ["audit report must be a JSON object"]
    if data.get("schema_version") != AUDIT_SCHEMA_VERSION:
        problems.append("unsupported schema_version")
    digest = data.get("digest")
    if not digest:
        problems.append("missing digest")
    else:
        payload = {k: v for k, v in data.items() if k != "digest"}
        if sha256_text(canonical(payload)) != digest:
            problems.append("digest mismatch: report was edited after generation")
    try:
        points = [ChangePoint.from_dict(p) for p in data.get("points", [])]
    except (KeyError, TypeError, ValueError) as exc:
        return False, [f"malformed change point: {exc}"]
    for point in points:
        if point.status not in _STATUSES:
            problems.append(f"point {point.index}: invalid status {point.status!r}")
            continue
        derived = _classify(
            RunRecord(
                point.exit_code,
                point.stdout_sha256,
                point.stderr_sha256,
                point.stdout_excerpt,
                point.stderr_excerpt,
                0.0,
                failure_kind=point.failure_kind,
                failure_evidence=point.failure_evidence,
            )
        )
        if derived != point.status:
            problems.append(
                f"point {point.index}: status {point.status!r} recomputes as {derived!r}"
            )
    baseline = data.get("baseline") or {}
    if baseline.get("exit_code") != 0:
        problems.append("baseline did not pass; the audit is unsound")
    return not problems, problems


def _sanitize(value: str) -> str:
    """Make PR-author-controlled diff/output text inert in Markdown."""
    escaped = (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "&#96;")
    )
    lines = []
    for line in escaped.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("#", ">", "|")):
            marker = stripped[0]
            line = line[: len(line) - len(stripped)] + f"&#{ord(marker)};" + stripped[1:]
        lines.append(line)
    return "\n".join(lines)


def render_audit_markdown(report: AuditReport) -> str:
    """A review note that separates measured facts from human judgement."""
    counted = (
        f"{report.tests_passed} tests passed"
        if report.tests_passed is not None
        else "test count not parseable for this runner"
    )
    counts = {status: sum(1 for p in report.points if p.status == status) for status in _STATUSES}
    lines = [
        "## Test-sensitivity audit: " + report.verdict,
        "",
        "Method: each changed hunk was reverted to its base value **one at a time**, "
        "with every other change kept at head, and the same test command was re-run. "
        "This is a reproducible experiment, not an inference about the specification.",
        "",
        f"Baseline (unmodified head): exit `{report.baseline.exit_code}`, {counted}. "
        f"Command: `{' '.join(report.command)}`.",
        "",
        f"Change points audited: {len(report.points)}. "
        f"PINNED {counts[STATUS_PINNED]} / UNPINNED {counts[STATUS_UNPINNED]} / "
        f"INCONCLUSIVE {counts[STATUS_INCONCLUSIVE]}.",
        "",
        "### PROVEN — measured, reproducible facts",
        "",
        "| # | Location | Revert result | Status |",
        "| --- | --- | --- | --- |",
    ]
    for point in report.points:
        location = f"{_sanitize(point.path)}:{point.new_start}"
        result = f"exit `{point.exit_code}`, {_sanitize(point.failure_kind)}"
        lines.append(f"| {point.index} | `{location}` | {result} | **{point.status}** |")
    lines += [
        "",
        "PINNED means reverting that point made a recognised assertion fail: at least "
        "one test observes it. UNPINNED means the entire suite still passed with that "
        "point removed: no test in this suite, under this command, distinguishes it. "
        "INCONCLUSIVE means the reverted run failed for a non-assertion reason; no claim is made.",
        "",
        "This is stronger than a whole-tree `VERIFIED`: four covered points can make "
        "the whole-tree check pass while a fifth is entirely unobserved.",
    ]

    gaps = [p for p in report.points if p.status == STATUS_UNPINNED]
    if gaps:
        lines += ["", "### UNPINNED points — exact detail", ""]
        for point in gaps:
            lines += [
                f"**Point {point.index}: `{_sanitize(point.path)}:{point.new_start}`**",
                "",
                "Reverted from:",
                "```",
                _sanitize("\n".join(point.head_lines)[:600]),
                "```",
                "Back to base:",
                "```",
                _sanitize("\n".join(point.base_lines)[:600]),
                "```",
                f"With that single hunk reverted the suite still exited "
                f"`{point.exit_code}` (stdout sha256 `{point.stdout_sha256[:16]}...`).",
                "",
                "### SUGGESTION — requires human judgement, NOT proven",
                "",
                "Add a test that executes this line and asserts a value that differs "
                "between the base and head text shown above. The tool cannot choose the "
                "expected value: that comes from the issue/spec, which is not in the diff. "
                "No tool can guarantee a suggested assertion is correct.",
                "",
                "Verify any such test deterministically by re-running the single-point audit:",
                "",
                "```shell",
                f"vrc audit --base {report.base_sha[:12]} --head {report.head_sha[:12]} "
                f"--point {point.index}",
                "```",
                "",
                "Accept it only if that point changes from UNPINNED to PINNED. That "
                "acceptance check IS reproducible and exact; the choice of assertion is not.",
                "",
            ]
    else:
        lines += ["", "### SUGGESTION — none required", "", "No UNPINNED point was found."]

    lines += [
        "",
        "### What this report cannot prove",
        "",
        "- That a PINNED test asserts the *correct* behaviour. A test can pin the wrong expected value.",
        "- That an UNPINNED point is wrong or unused. It only says this test suite does not observe it.",
        "- That the change is semantically correct, secure, or well designed. A human still reviews.",
        "- Any property outside the exact command, test set and environment recorded above.",
    ]
    if report.unaudited_points:
        lines += [
            "",
            f"**INCOMPLETE:** {report.unaudited_points} change point(s) were not audited "
            "(`--max-points`). No coverage claim is made for them.",
        ]
    if report.notes:
        lines += ["", "Notes:"] + [f"- {_sanitize(note)}" for note in report.notes]
    lines += [
        "",
        f"Verifier `vrc {report.verifier_version}`. Reproduce with "
        f"`vrc audit --base {report.base_sha[:12]} --head {report.head_sha[:12]}`; "
        "validate integrity with `vrc audit-check audit.json`.",
        "",
    ]
    if report.origin == "ci":
        lines.append("Generated by maintainer-controlled CI.")
    else:
        lines.append(
            "Generated locally with `vrc audit`. This is a **self-check**, not a CI attestation."
        )
    return "\n".join(lines) + "\n"
