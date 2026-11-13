"""Regressions from a real attempt to use the action on langchain-ai/langgraph.

That attempt exposed three concrete defects: a monorepo has no way to run its
tests from a subpackage, an unknown policy key (``setup-command``) is silently
ignored, and there is no way to check a policy locally before asking a
maintainer to approve a workflow. This module pins all three fixes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import BUGGY_MODULE, CATCHING_TEST, FIXED_MODULE, VACUOUS_TEST, git
from vrc.cli import main
from vrc.config import Config, ConfigError
from vrc.receipt import Receipt, RunRecord, derive_verdict
from vrc.replay import ReplayError, replay
from vrc.report import render_markdown


def _policy(command: list[str], **extra: object) -> bytes:
    fields: dict[str, object] = {"command": command, "test_paths": ["svc/tests/"]}
    fields.update(extra)
    return (
        "\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items()) + "\n"
    ).encode()


def _monorepo(
    tmp_path: Path, python_command: list[str], *, extra: str = ""
) -> tuple[Path, str, str]:
    repo = tmp_path / "monorepo"
    (repo / "svc" / "tests").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "vrc@example.invalid")
    git(repo, "config", "user.name", "vrc test")

    policy = (
        f"command = {json.dumps([*python_command, 'tests/test_sample.py'])}\n"
        'working_directory = "svc"\n'
        'test_paths = ["svc/tests/"]\n'
        "timeout_seconds = 120\n" + extra
    )
    (repo / ".vrc.toml").write_text(policy, encoding="utf-8")
    (repo / "svc" / "sample.py").write_text(BUGGY_MODULE, encoding="utf-8")
    (repo / "svc" / "tests" / "test_sample.py").write_text(VACUOUS_TEST, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").strip()

    (repo / "svc" / "sample.py").write_text(FIXED_MODULE, encoding="utf-8")
    (repo / "svc" / "tests" / "test_sample.py").write_text(CATCHING_TEST, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix")
    head = git(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


# --- working_directory -------------------------------------------------------


def test_working_directory_defaults_to_the_repository_root() -> None:
    assert Config.default().working_directory == ""
    assert Config.load(_policy(["python"])).working_directory == ""


def test_working_directory_accepts_a_nested_relative_path() -> None:
    config = Config.load(_policy(["python"], working_directory="libs/langgraph"))
    assert config.working_directory == "libs/langgraph"


@pytest.mark.parametrize(
    "bad", ["../outside", "/absolute", "svc/../../etc", "svc//tests", "svc\\tests", "svc/./tests"]
)
def test_working_directory_rejects_unsafe_paths(bad: str) -> None:
    with pytest.raises(ConfigError, match="working_directory"):
        Config.load(_policy(["python"], working_directory=bad))


def test_working_directory_must_be_a_string() -> None:
    with pytest.raises(ConfigError, match="working_directory"):
        Config.load(_policy(["python"], working_directory=["svc"]))


def test_replay_runs_the_command_from_the_working_directory(
    tmp_path: Path, python_command: list[str]
) -> None:
    repo, base, head = _monorepo(tmp_path, python_command)
    outcome = replay(repo, base, head, workdir=tmp_path / "work")

    assert outcome.receipt.verdict == "VERIFIED"
    assert outcome.receipt.environment["working_directory"] == "svc"
    # The source change still must not leak onto the base tree.
    assert (outcome.base_dir / "svc" / "sample.py").read_text(encoding="utf-8") == BUGGY_MODULE


def test_missing_working_directory_is_a_hard_error_not_a_false_rerun(
    tmp_path: Path, python_command: list[str]
) -> None:
    """The policy on the BASE ref names a directory that does not exist."""
    repo = tmp_path / "bad-workdir"
    (repo / "svc" / "tests").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "vrc@example.invalid")
    git(repo, "config", "user.name", "vrc test")
    (repo / ".vrc.toml").write_text(
        f"command = {json.dumps([*python_command, 'tests/test_sample.py'])}\n"
        'working_directory = "missing-dir"\n'
        'test_paths = ["svc/tests/"]\n'
        "timeout_seconds = 120\n",
        encoding="utf-8",
    )
    (repo / "svc" / "sample.py").write_text(BUGGY_MODULE, encoding="utf-8")
    (repo / "svc" / "tests" / "test_sample.py").write_text(VACUOUS_TEST, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base with broken policy")
    base = git(repo, "rev-parse", "HEAD").strip()

    (repo / "svc" / "sample.py").write_text(FIXED_MODULE, encoding="utf-8")
    (repo / "svc" / "tests" / "test_sample.py").write_text(CATCHING_TEST, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix")
    head = git(repo, "rev-parse", "HEAD").strip()

    with pytest.raises(ReplayError, match="working_directory"):
        replay(repo, base, head, workdir=tmp_path / "work2")


# --- unknown keys ------------------------------------------------------------


def test_unknown_policy_key_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown policy key"):
        Config.load(_policy(["python"], test_path=["tests/"]))


def test_setup_command_in_policy_has_an_actionable_error() -> None:
    with pytest.raises(ConfigError) as exc:
        Config.load(_policy(["python"], **{"setup-command": ["pip", "install", "pytest"]}))
    message = str(exc.value)
    assert "setup-command" in message
    assert "`with:`" in message
    assert "unknown policy key" in message


# --- vrc validate (local preflight) -----------------------------------------


def test_validate_accepts_a_usable_policy(tmp_path: Path, capsys) -> None:
    repo, _, _ = _monorepo(tmp_path, ["python", "-m", "pytest", "-q"])
    code = main(["validate", str(repo / ".vrc.toml"), "--repo", str(repo)])
    assert code == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "svc" in out


def test_validate_rejects_an_invalid_policy(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".vrc.toml"
    path.write_text('command = ["bash", "-lc", "cd svc && pytest"]\n', encoding="utf-8")
    assert main(["validate", str(path), "--repo", str(tmp_path)]) == 2
    assert "not allowlisted" in capsys.readouterr().err


def test_validate_detects_a_missing_working_directory(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".vrc.toml"
    path.write_text(
        'command = ["python", "-m", "pytest", "-q"]\n'
        'working_directory = "does-not-exist"\n'
        'test_paths = ["tests/"]\n',
        encoding="utf-8",
    )
    assert main(["validate", str(path), "--repo", str(tmp_path)]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_validate_detects_missing_test_paths(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".vrc.toml"
    path.write_text(
        'command = ["python", "-m", "pytest", "-q"]\ntest_paths = ["tests/"]\n',
        encoding="utf-8",
    )
    assert main(["validate", str(path), "--repo", str(tmp_path)]) == 1
    assert "test path" in capsys.readouterr().err.lower()


# --- honest reporting --------------------------------------------------------


def _receipt(environment: dict[str, str], *, untrusted: bool = False) -> Receipt:
    base = RunRecord(0, "0" * 64, "0" * 64, "", "", 1.0, failure_kind="none")
    head = RunRecord(0, "0" * 64, "0" * 64, "", "", 1.0, failure_kind="none")
    verdict, reason = derive_verdict(base, head, untrusted)
    return Receipt(
        "repo",
        "a" * 40,
        "b" * 40,
        ("python", "-m", "pytest"),
        ("tests",),
        {"tests/t.py": "c" * 64},
        "d" * 64,
        environment,
        base,
        head,
        "2026-11-13T00:00:00+00:00",
        verdict,
        reason,
        protected_touched=untrusted,
    )


def test_local_receipt_is_not_labelled_as_maintainer_ci() -> None:
    rendered = render_markdown(_receipt({"origin": "local"}))
    assert "maintainer-controlled CI" not in rendered
    assert "self-check" in rendered


def test_locally_generated_receipt_without_origin_is_treated_as_local() -> None:
    rendered = render_markdown(_receipt({}))
    assert "maintainer-controlled CI" not in rendered
    assert "self-check" in rendered


def test_ci_receipt_is_labelled_as_maintainer_ci() -> None:
    rendered = render_markdown(_receipt({"origin": "ci", "github_run_id": "42"}))
    assert "maintainer-controlled CI" in rendered
    assert "self-check" not in rendered


def test_untrusted_report_explains_the_bootstrap_paradox() -> None:
    rendered = render_markdown(_receipt({"origin": "ci"}, untrusted=True))
    assert "separate PR" in rendered
    assert "UNTRUSTED" in rendered
