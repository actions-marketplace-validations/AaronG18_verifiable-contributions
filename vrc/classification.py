"""Classify *why* a test command failed.

A non-zero exit code is not evidence on its own. A missing dependency, a
collection error and a genuine assertion failure all produce exit code 1, but
only the last one demonstrates that a test detected the change under review.

Markers are deliberately conservative and cover common runners. Repositories
can append their own via the policy file; anything unrecognised is UNKNOWN and
is never treated as evidence.
"""

from __future__ import annotations

# Signals that the tests did not actually run. Checked first: if the suite
# could not even be collected, no assertion anywhere in the output counts.
DEFAULT_ENVIRONMENT_MARKERS: tuple[str, ...] = (
    "modulenotfounderror",
    "importerror",
    "cannot import name",
    "no module named",
    "error collecting",
    "error: collecting",
    "import error",
    "no tests ran",
    "no tests collected",
    "collected 0 items",
    "found no collectors",
    "file or directory not found",
    "no such file or directory",
    "command not found",
    "not recognized as an internal or external command",
    "syntaxerror",
    "indentationerror",
    "internalerror",
    "cannot find module",
    "err_module_not_found",
    "could not start command",
    # A base run that fails to connect/start/load before any test executes is an
    # environment error, not evidence that a test detected the change. These are
    # checked before the assertion markers (which include the broad "failed "
    # substring), so a line like "Failed to connect to the database" is
    # classified as `environment` -> NEEDS_RERUN instead of a false VERIFIED.
    "failed to connect",
    "failed to start",
    "failed to load",
    "failed to resolve",
)

# Signals that a test executed and its assertion failed.
DEFAULT_ASSERTION_MARKERS: tuple[str, ...] = (
    "assertionerror",
    "assert ",
    "failed ",
    " fail:",
    "--- fail:",
    "= failures =",
    "short test summary info",
    "1 failed",
    "failed (failures",
    "expect(",
    "comparisondifference",
    "not equal to expected",
    "test failure",
)

KIND_NONE = "none"
KIND_ASSERTION = "assertion"
KIND_ENVIRONMENT = "environment"
KIND_UNKNOWN = "unknown"

_KINDS = (KIND_NONE, KIND_ASSERTION, KIND_ENVIRONMENT, KIND_UNKNOWN)


def is_valid_kind(value: str) -> bool:
    return value in _KINDS


# Extra markers for common test runners, selected via the policy's `runner`
# list. They are *appended* to the defaults (never replacing them), so a runner
# preset cannot switch off the environment-error detection. Unknown runners are
# ignored and fall back to the defaults plus any `assertion_markers` /
# `environment_markers` the policy appends directly.
# Only signatures that appear when a test has actually FAILED belong here.
# Success tokens (check marks, "passing", "N passed"), crash tokens ("panic:",
# "error:") and vague words ("expected", "to be", "failed") must NOT be used:
# a base run can exit non-zero for an unrelated reason while still printing a
# green summary, and reading that as an assertion yields a false VERIFIED.
# tests/test_no_false_positive.py enforces this structurally.
RUNNER_ASSERTION_MARKERS: dict[str, tuple[str, ...]] = {
    "jest": ("✕", "✗", "×", "expect(", "assertionerror"),
    "vitest": ("✗", "×", "expect(", "assertionerror"),
    "go-test": ("--- fail:", "--- fail", "error trace:", "not equal"),
    "mocha": ("failing", "✗", "assertionerror"),
    "npm": ("failing", "✗", "assertionerror", "not equal"),
}

RUNNER_ENVIRONMENT_MARKERS: dict[str, tuple[str, ...]] = {
    "jest": (
        "cannot find module",
        "no tests found",
        "syntaxerror",
        "cannot use import statement",
        "transform failed",
    ),
    "vitest": (
        "cannot find module",
        "failed to resolve import",
        "no test files found",
        "syntaxerror",
        "failed to load url",
    ),
    "go-test": (
        "build failed",
        "cannot find package",
        "no required module provides package",
        "cannot load",
        "command-line-arguments",
        "no go files",
        # A panic is a crash, not an assertion that detected the change.
        "panic:",
        "runtime error:",
    ),
    "mocha": (
        "cannot find module",
        "no files found",
        "syntaxerror",
    ),
    "npm": (
        "npm error code",
        "missing script: test",
        "eresolve",
        "npm error",
        "command not found",
    ),
}

# A line that explicitly reports zero failures cannot serve as evidence of a
# failure, even when a broad marker also matches it (e.g. mocha's "0 failing").
NO_FAILURE_MARKERS: tuple[str, ...] = (
    "0 failed",
    "0 failing",
    "0 errors",
    "no failures",
    "all tests passed",
    "all passed",
)


def markers_for(
    runners: tuple[str, ...] = (),
    extra_environment: tuple[str, ...] = (),
    extra_assertion: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve the final (environment, assertion) marker tuples.

    Starts from the built-in defaults, appends each known runner's preset, then
    appends any explicit extra markers. Unknown runners contribute nothing.
    """
    environment = list(DEFAULT_ENVIRONMENT_MARKERS)
    assertion = list(DEFAULT_ASSERTION_MARKERS)
    for runner in runners:
        environment += list(RUNNER_ENVIRONMENT_MARKERS.get(runner, ()))
        assertion += list(RUNNER_ASSERTION_MARKERS.get(runner, ()))
    environment += list(extra_environment)
    assertion += list(extra_assertion)
    return tuple(environment), tuple(assertion)


def classify_failure(
    exit_code: int,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool,
    environment_markers: tuple[str, ...] = DEFAULT_ENVIRONMENT_MARKERS,
    assertion_markers: tuple[str, ...] = DEFAULT_ASSERTION_MARKERS,
) -> tuple[str, str]:
    """Return (kind, evidence). Evidence is one matching line, for a reviewer."""
    if exit_code == 0:
        return KIND_NONE, ""
    if timed_out:
        return KIND_ENVIRONMENT, "the command exceeded its timeout and was terminated"

    blob = f"{stdout}\n{stderr}".lower()
    lines = [line.strip() for line in blob.splitlines() if line.strip()]

    # The evidence is the matching line verbatim, not a description of it.
    # `check_receipt` re-runs classify_failure() on this string so a receipt
    # whose failure_kind was edited can be detected.
    for marker in environment_markers:
        for line in lines:
            if marker in line:
                return KIND_ENVIRONMENT, line[:300]

    for marker in assertion_markers:
        for line in lines:
            if marker in line and not any(no_failure in line for no_failure in NO_FAILURE_MARKERS):
                return KIND_ASSERTION, line[:300]

    return KIND_UNKNOWN, ""
