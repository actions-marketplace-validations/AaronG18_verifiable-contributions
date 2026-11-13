"""Per-change-point test-sensitivity audit.

A whole-tree VERIFIED only proves that *some* test fails when the entire change
is removed. It cannot tell a maintainer which of several changed lines are
actually observed by a test. These tests pin the per-hunk mutation audit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.helpers import git
from vrc.audit import (
    STATUS_INCONCLUSIVE,
    STATUS_PINNED,
    STATUS_UNPINNED,
    AuditError,
    check_audit,
    render_audit_markdown,
)
from vrc.cli import main
from vrc.replay import replay

BUGGY = """def alpha(x):
    return x + 1


def beta(x):
    return x + 1


def gamma(x):
    return x + 1
"""
FIXED = BUGGY.replace("x + 1", "x + 2")


def _policy(command: list[str]) -> str:
    return f'command = {json.dumps(command)}\ntest_paths = ["tests/"]\ntimeout_seconds = 120\n'


def _repo(tmp_path: Path, *, head_test: str, policy: str | None = None) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "vrc@example.invalid")
    git(repo, "config", "user.name", "vrc test")
    (repo / ".vrc.toml").write_text(policy or "", encoding="utf-8")
    (repo / "sample.py").write_text(BUGGY, encoding="utf-8")
    (repo / "tests" / "test_sample.py").write_text(head_test, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").strip()

    (repo / "sample.py").write_text(FIXED, encoding="utf-8")
    (repo / "tests" / "test_sample.py").write_text(head_test, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix three independent lines")
    head = git(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


TEST_TWO = """from sample import alpha, beta


def test_alpha():
    assert alpha(1) == 3


def test_beta():
    assert beta(1) == 3
"""
TEST_THREE = (
    TEST_TWO.replace("alpha, beta", "alpha, beta, gamma")
    + """


def test_gamma():
    assert gamma(1) == 3
"""
)


def _audit(repo: Path, base: str, head: str, workdir: Path, **kwargs):
    from vrc.audit import audit

    return audit(repo, base, head, workdir=workdir, **kwargs)


# --- the core gap ------------------------------------------------------------


def test_audit_detects_the_one_uncovered_change_point(
    tmp_path: Path, python_command: list[str]
) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    report = _audit(repo, base, head, tmp_path / "work")

    assert len(report.points) == 3, "three independent hunks must be three change points"
    statuses = [p.status for p in report.points]
    assert statuses.count(STATUS_PINNED) == 2
    assert statuses.count(STATUS_UNPINNED) == 1
    assert report.verdict == "GAPS_FOUND"
    gap = next(p for p in report.points if p.status == STATUS_UNPINNED)
    assert gap.path == "sample.py"
    assert "gamma" in "".join(gap.head_lines) or "x + 2" in "".join(gap.head_lines)
    assert gap.new_start >= 10, "the gamma hunk is the last of the three"


def test_whole_tree_verified_masks_the_gap(tmp_path: Path, python_command: list[str]) -> None:
    """The exact false confidence the audit exists to remove."""
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    outcome = replay(repo, base, head, workdir=tmp_path / "replay")
    assert outcome.receipt.verdict == "VERIFIED"

    report = _audit(repo, base, head, tmp_path / "audit")
    assert report.verdict == "GAPS_FOUND"
    assert any(p.status == STATUS_UNPINNED for p in report.points)


def test_all_points_pinned_when_every_change_is_covered(
    tmp_path: Path, python_command: list[str]
) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_THREE, policy=_policy(python_command))
    report = _audit(repo, base, head, tmp_path / "work")
    assert len(report.points) == 3
    assert all(p.status == STATUS_PINNED for p in report.points)
    assert report.verdict == "ALL_PINNED"


# --- "are those four PASSes real?" ------------------------------------------


def test_baseline_must_actually_pass(tmp_path: Path, python_command: list[str]) -> None:
    failing = TEST_TWO + "\n\ndef test_always_fails():\n    assert False\n"
    repo, base, head = _repo(tmp_path, head_test=failing, policy=_policy(python_command))
    with pytest.raises(AuditError, match="baseline"):
        _audit(repo, base, head, tmp_path / "work")


def test_zero_collected_tests_is_unsound(tmp_path: Path) -> None:
    """exit 0 with no tests collected is a fake PASS, not a green suite."""
    fake = [sys.executable, "-c", "print('collected 0 items')"]
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(fake))
    with pytest.raises(AuditError, match="no tests"):
        _audit(repo, base, head, tmp_path / "work")


def test_failing_revert_with_unrecognised_output_is_inconclusive(
    tmp_path: Path,
) -> None:
    """A revert that only breaks imports is NOT evidence of a test."""
    command = [
        sys.executable,
        "-c",
        (
            "import sys,pathlib;"
            "p=pathlib.Path('sample.py').read_text();"
            "sys.exit(1) if 'x + 1' in p else sys.exit(0)"
        ),
    ]
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(command))
    report = _audit(repo, base, head, tmp_path / "work")
    assert all(p.status == STATUS_INCONCLUSIVE for p in report.points)
    assert report.verdict == "INCONCLUSIVE"


def test_baseline_records_how_many_tests_ran(tmp_path: Path, python_command: list[str]) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    report = _audit(repo, base, head, tmp_path / "work")
    assert report.baseline.exit_code == 0
    assert report.tests_passed and report.tests_passed >= 2


# --- scope and honesty -------------------------------------------------------


def test_test_paths_and_protected_paths_are_not_change_points(
    tmp_path: Path, python_command: list[str]
) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    (repo / "SECURITY.md").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "touch a protected path")
    head = git(repo, "rev-parse", "HEAD").strip()

    report = _audit(repo, base, head, tmp_path / "work")
    paths = {p.path for p in report.points}
    assert paths == {"sample.py"}, f"unexpected change points: {paths}"
    assert any("SECURITY.md" in note for note in report.notes)


def test_max_points_marks_the_rest_unaudited(tmp_path: Path, python_command: list[str]) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    report = _audit(repo, base, head, tmp_path / "work", max_points=1)
    assert len(report.points) == 1
    assert report.unaudited_points == 2
    assert report.verdict == "INCOMPLETE"
    assert any("unaudited" in note.lower() for note in report.notes)


# --- report integrity and wording -------------------------------------------


def test_digest_detects_a_tampered_status(tmp_path: Path, python_command: list[str]) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    data = _audit(repo, base, head, tmp_path / "work").to_dict()
    assert check_audit(data)[0]
    data["points"][0]["status"] = STATUS_UNPINNED
    ok, problems = check_audit(data)
    assert not ok and any("digest" in p or "status" in p for p in problems)


def test_report_separates_proven_facts_from_suggestions(
    tmp_path: Path, python_command: list[str]
) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    report = _audit(repo, base, head, tmp_path / "work")
    md = render_audit_markdown(report)
    assert "PROVEN" in md
    assert "SUGGESTION" in md
    assert "cannot prove" in md.lower()
    assert "collected" in md.lower() or "passed" in md.lower()
    # A gap must come with a reproducible command, not just an opinion.
    assert "--point" in md
    assert "vrc audit" in md


def test_report_escapes_untrusted_diff_text(tmp_path: Path, python_command: list[str]) -> None:
    from tests.helpers import git as _git

    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    (repo / "sample.py").write_text(FIXED + "\n# <script>alert(1)</script>\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add hostile comment")
    head = _git(repo, "rev-parse", "HEAD").strip()
    md = render_audit_markdown(_audit(repo, base, head, tmp_path / "work2"))
    assert "<script>" not in md


# --- CLI ---------------------------------------------------------------------


def test_cli_audit_markdown_and_fail_on(tmp_path: Path, python_command: list[str]) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    out = tmp_path / "audit.json"
    code = main(
        [
            "audit",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--output",
            str(out),
            "--format",
            "markdown",
        ]
    )
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "GAPS_FOUND"
    code = main(
        [
            "audit",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--output",
            str(tmp_path / "a2.json"),
            "--format",
            "summary",
            "--fail-on",
            "GAPS_FOUND",
        ]
    )
    assert code == 1


def test_cli_audit_check(tmp_path: Path, python_command: list[str], capsys) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    out = tmp_path / "audit.json"
    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--base",
                base,
                "--head",
                head,
                "--output",
                str(out),
                "--format",
                "summary",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["audit-check", str(out)]) == 0
    assert "OK" in capsys.readouterr().out


# --- single-point reproduction (the promise made by the report) -------------


def test_single_point_reproduction_matches_the_full_audit(
    tmp_path: Path, python_command: list[str]
) -> None:
    from vrc.audit import audit

    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    full = audit(repo, base, head, workdir=tmp_path / "full")
    for point in full.points:
        single = audit(
            repo, base, head, workdir=tmp_path / f"p{point.index}", only_point=point.index
        )
        assert len(single.points) == 1
        assert single.points[0].index == point.index
        assert single.points[0].status == point.status
        assert single.points[0].exit_code == point.exit_code


def test_single_point_out_of_range_is_an_error(tmp_path: Path, python_command: list[str]) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    with pytest.raises(AuditError, match="out of range"):
        _audit(repo, base, head, tmp_path / "work", only_point=99)


def test_cli_point_flag_reproduces_one_claim(tmp_path: Path, python_command: list[str]) -> None:
    repo, base, head = _repo(tmp_path, head_test=TEST_TWO, policy=_policy(python_command))
    out = tmp_path / "one.json"
    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--base",
                base,
                "--head",
                head,
                "--output",
                str(out),
                "--format",
                "summary",
                "--point",
                "3",
            ]
        )
        == 0
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["points"]) == 1
    assert data["points"][0]["index"] == 3
    assert data["points"][0]["status"] == STATUS_UNPINNED
