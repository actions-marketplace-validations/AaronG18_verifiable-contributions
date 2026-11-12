"""A false VERIFIED is worse than a false NEEDS_RERUN.

These tests assert that output which reports *passing* tests can never be read
as evidence of an assertion failure, even when the command exits non-zero for
an unrelated reason.
"""

from __future__ import annotations

import pytest

from vrc.classification import (
    KIND_ASSERTION,
    KIND_ENVIRONMENT,
    RUNNER_ASSERTION_MARKERS,
    classify_failure,
    markers_for,
)
from vrc.receipt import VERDICT_VERIFIED, RunRecord, derive_verdict

SUCCESS_OUTPUTS: tuple[tuple[str, str], ...] = (
    (
        "jest",
        "PASS ./src/add.test.js\nTest Suites: 1 passed, 1 total\nTests: 2 passed, 2 total",
    ),
    (
        "vitest",
        " ✓ tests/foo.test.ts (3 tests) 12ms\n Test Files  1 passed (1)\n Tests  3 passed (3)",
    ),
    ("mocha", "  3 passing (4ms)\n  0 failing"),
    ("go-test", "ok  \texample.com/pkg\t0.005s\nPASS"),
    ("npm", "> test\nAll tests passed (2 passed, 0 failing)"),
)


@pytest.mark.parametrize("runner,output", SUCCESS_OUTPUTS)
def test_success_output_is_never_assertion(runner: str, output: str) -> None:
    env, assertion = markers_for(runners=(runner,))
    kind, _ = classify_failure(
        1, output, "", timed_out=False, environment_markers=env, assertion_markers=assertion
    )
    assert kind != KIND_ASSERTION, f"{runner} passing output was read as an assertion failure"


@pytest.mark.parametrize("runner,output", SUCCESS_OUTPUTS)
def test_success_output_cannot_produce_verified(runner: str, output: str) -> None:
    """End-to-end: base fails for an unrelated reason but prints a green summary."""
    env, assertion = markers_for(runners=(runner,))
    kind, evidence = classify_failure(
        1,
        output + "\nsh: line 1: 4201 Killed",
        "",
        timed_out=False,
        environment_markers=env,
        assertion_markers=assertion,
    )
    base = RunRecord(
        1, "0" * 64, "0" * 64, "", "", 1.0, failure_kind=kind, failure_evidence=evidence
    )
    head = RunRecord(0, "0" * 64, "0" * 64, "", "", 1.0, failure_kind="none")
    verdict, _ = derive_verdict(base, head, False)
    assert verdict != VERDICT_VERIFIED, f"{runner} produced a false VERIFIED"


def test_panic_is_an_environment_failure_not_an_assertion() -> None:
    env, assertion = markers_for(runners=("go-test",))
    kind, _ = classify_failure(
        1,
        "panic: runtime error: index out of range [3] with length 2",
        "",
        timed_out=False,
        environment_markers=env,
        assertion_markers=assertion,
    )
    assert kind == KIND_ENVIRONMENT


def test_zero_failures_line_is_not_evidence() -> None:
    env, assertion = markers_for(runners=("mocha",))
    kind, _ = classify_failure(
        1,
        "  3 passing (4ms)\n  0 failing",
        "",
        timed_out=False,
        environment_markers=env,
        assertion_markers=assertion,
    )
    assert kind != KIND_ASSERTION


# Structural guard: adding a vague/success token back to a preset must fail CI.
_FORBIDDEN_ASSERTION_TOKENS = (
    "✓",
    "❯",
    "●",
    "passing",
    "tests: ",
    "test suites:",
    "tests ",
    "panic:",
    "error:",
    "expected",
    "received",
    "actual",
    "to be",
    "to equal",
    "to match",
    "failed",
)


def test_runner_presets_contain_no_success_or_vague_tokens() -> None:
    for runner, markers in RUNNER_ASSERTION_MARKERS.items():
        for marker in markers:
            for token in _FORBIDDEN_ASSERTION_TOKENS:
                assert token not in marker, (
                    f"runner {runner!r} has unsafe assertion marker {marker!r} (contains {token!r})"
                )
