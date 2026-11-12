from __future__ import annotations

from vrc.classification import (
    KIND_ASSERTION,
    KIND_ENVIRONMENT,
    KIND_NONE,
    KIND_UNKNOWN,
    classify_failure,
    is_valid_kind,
    markers_for,
)


def test_successful_run_is_none() -> None:
    kind, evidence = classify_failure(0, "1 passed", "", timed_out=False)
    assert kind == KIND_NONE
    assert evidence == ""


def test_pytest_assertion_failure_is_evidence() -> None:
    out = "FAILED tests/test_sample.py::test_double - assert 9 == 8\n1 failed in 0.1s"
    kind, evidence = classify_failure(1, out, "E   AssertionError", timed_out=False)
    assert kind == KIND_ASSERTION
    assert "assertionerror" in evidence.lower()


def test_import_error_is_not_evidence_even_with_assertion_text() -> None:
    out = (
        "ERROR collecting tests/test_sample.py\n"
        "ImportError while importing test module '/tmp/base/tests/test_sample.py'.\n"
        "E   ModuleNotFoundError: No module named 'sample'\n"
        "FAILED tests/test_sample.py::test_double\n"
    )
    kind, evidence = classify_failure(2, out, "", timed_out=False)
    assert kind == KIND_ENVIRONMENT
    assert "modulenotfounderror" in evidence.lower()


def test_no_tests_collected_is_environment_error() -> None:
    kind, _ = classify_failure(4, "collected 0 items / 1 error", "", timed_out=False)
    assert kind == KIND_ENVIRONMENT


def test_unrecognised_failure_stays_unknown() -> None:
    kind, evidence = classify_failure(1, "runner exited with a strange status", "", timed_out=False)
    assert kind == KIND_UNKNOWN
    assert evidence == ""


def test_timeout_is_environment_error() -> None:
    kind, _ = classify_failure(124, "FAILED something", "", timed_out=True)
    assert kind == KIND_ENVIRONMENT


def test_failed_to_connect_is_environment_not_assertion() -> None:
    # "failed " is a broad assertion substring; a startup/connection failure
    # that prints "Failed to connect to the database" must not read as evidence.
    kind, _ = classify_failure(
        1, "", "Failed to connect to database: connection refused", timed_out=False
    )
    assert kind == KIND_ENVIRONMENT


def test_failed_to_start_is_environment_not_assertion() -> None:
    kind, _ = classify_failure(1, "", "Failed to start server before tests", timed_out=False)
    assert kind == KIND_ENVIRONMENT


def test_failed_to_load_is_environment_not_assertion() -> None:
    kind, _ = classify_failure(1, "", "Failed to load configuration file", timed_out=False)
    assert kind == KIND_ENVIRONMENT


def test_real_pytest_assertion_still_recognised() -> None:
    # The fix must not regress genuine pytest assertion detection.
    out = "FAILED tests/test_sample.py::test_double - assert 9 == 8\n1 failed in 0.1s"
    kind, evidence = classify_failure(1, out, "E   AssertionError", timed_out=False)
    assert kind == KIND_ASSERTION


def test_custom_markers_are_appended() -> None:
    kind, _ = classify_failure(
        1,
        "vitest: FAIL | expected 2 to be 3",
        "",
        timed_out=False,
        assertion_markers=("fail |",),
    )
    assert kind == KIND_ASSERTION


def test_is_valid_kind() -> None:
    assert all(
        is_valid_kind(k) for k in (KIND_NONE, KIND_ASSERTION, KIND_ENVIRONMENT, KIND_UNKNOWN)
    )
    assert not is_valid_kind("asserted")


# --- Multi-runner support: each runner's assertion output is recognised. ---


def test_jest_assertion_failure_is_evidence() -> None:
    out = "FAIL ./src/add.test.js\n  ✕ adds numbers (3 ms)\n  expect(received).toBe(3)\n    Expected: 3\n    Received: 2"
    env, assert_ = markers_for(runners=("jest",))
    kind, evidence = classify_failure(
        1, out, "", timed_out=False, environment_markers=env, assertion_markers=assert_
    )
    assert kind == KIND_ASSERTION


def test_vitest_assertion_failure_is_evidence() -> None:
    out = "FAIL src/add.test.ts > adds numbers\n × adds numbers\n   AssertionError: expected 2 to be 3"
    env, assert_ = markers_for(runners=("vitest",))
    kind, _ = classify_failure(
        1, out, "", timed_out=False, environment_markers=env, assertion_markers=assert_
    )
    assert kind == KIND_ASSERTION


def test_go_test_assertion_failure_is_evidence() -> None:
    out = "--- FAIL: TestAdd (0.00s)\n    add_test.go:12: expected 3, got 2\nFAIL\nexit status 1"
    env, assert_ = markers_for(runners=("go-test",))
    kind, _ = classify_failure(
        1, out, "", timed_out=False, environment_markers=env, assertion_markers=assert_
    )
    assert kind == KIND_ASSERTION


def test_mocha_assertion_failure_is_evidence() -> None:
    out = "  1) add\n       should add numbers:\n         AssertionError: expected 2 to equal 3\n\n  1 failing"
    env, assert_ = markers_for(runners=("mocha",))
    kind, _ = classify_failure(
        1, out, "", timed_out=False, environment_markers=env, assertion_markers=assert_
    )
    assert kind == KIND_ASSERTION


def test_npm_assertion_failure_is_evidence() -> None:
    out = "FAIL src/index.test.js\n  ✗ exports the handler\n  AssertionError: expected true to be false\n1 failing"
    env, assert_ = markers_for(runners=("npm",))
    kind, _ = classify_failure(
        1, out, "", timed_out=False, environment_markers=env, assertion_markers=assert_
    )
    assert kind == KIND_ASSERTION


def test_go_build_failure_is_environment_error() -> None:
    out = 'package main: build failed\n# command-line-arguments\ncannot find package "example"'
    env, assert_ = markers_for(runners=("go-test",))
    kind, _ = classify_failure(
        2, out, "", timed_out=False, environment_markers=env, assertion_markers=assert_
    )
    assert kind == KIND_ENVIRONMENT


def test_jest_cannot_find_module_is_environment_error() -> None:
    out = "Cannot find module 'left-pad' from 'src/add.test.js'"
    env, assert_ = markers_for(runners=("jest",))
    kind, _ = classify_failure(
        1, out, "", timed_out=False, environment_markers=env, assertion_markers=assert_
    )
    assert kind == KIND_ENVIRONMENT


def test_unknown_runner_is_ignored_and_still_fails_closed() -> None:
    env, assert_ = markers_for(runners=("totally-unknown-runner",))
    # Without a matching assertion marker the failure stays UNKNOWN.
    kind, _ = classify_failure(
        1,
        "weird runner output with no known signature",
        "",
        timed_out=False,
        environment_markers=env,
        assertion_markers=assert_,
    )
    assert kind == KIND_UNKNOWN


def test_markers_for_appends_without_replacing_defaults() -> None:
    env, assert_ = markers_for(runners=("jest",))
    # Built-in pytest markers are still present (never replaced by a preset).
    assert "assertionerror" in assert_
    assert "modulenotfounderror" in env
    # And the jest preset is added on top.
    assert any("expect(" in m for m in assert_)
