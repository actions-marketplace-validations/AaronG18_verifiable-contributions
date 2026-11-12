"""Release and security contracts that must hold on every commit."""

from __future__ import annotations

import tomllib
from pathlib import Path

from vrc import __version__
from vrc.config import DEFAULT_PROTECTED_PATHS
from vrc.receipt import Receipt, RunRecord
from vrc.report import render_markdown

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_version_matches_runtime_version() -> None:
    assert _pyproject()["project"]["version"] == __version__


def test_version_is_not_behind_the_latest_release_tag() -> None:
    """Receipts must never claim an older verifier than the one actually shipped."""
    import subprocess

    def as_tuple(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split("."))

    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "tag", "--sort=-version:refname"],
        capture_output=True,
        check=False,
        text=True,
    )
    tags = [t.strip().lstrip("v") for t in proc.stdout.splitlines() if t.strip()]
    if tags:
        assert as_tuple(__version__) >= as_tuple(tags[0]), (
            f"__version__ {__version__} is older than the released tag v{tags[0]}"
        )


def test_all_workflows_are_protected() -> None:
    workflows = (REPO_ROOT / ".github/workflows").glob("*.yml")
    for workflow in workflows:
        rel = workflow.relative_to(REPO_ROOT).as_posix()
        assert any(rel.startswith(prefix.rstrip("/")) for prefix in DEFAULT_PROTECTED_PATHS), (
            f"{rel} is not covered by protected_paths"
        )


def test_repo_policy_matches_defaults() -> None:
    policy = tomllib.loads((REPO_ROOT / ".vrc.toml").read_text(encoding="utf-8"))
    assert tuple(policy["protected_paths"]) == DEFAULT_PROTECTED_PATHS


def test_action_validates_its_own_receipt() -> None:
    action = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")
    assert "vrc check receipt.json" in action, "the action must self-validate the receipt"
    # The check must gate the summary/artifact, not be a best-effort log line.
    check_at = action.index("vrc check receipt.json")
    explain_at = action.index("vrc explain receipt.json")
    upload_at = action.index("- name: Upload receipt")
    assert check_at < explain_at < upload_at
    assert 'echo "::error::receipt failed' in action


def test_workflow_yaml_is_parseable() -> None:
    """Every workflow must be valid YAML and keep the required `verify` job."""
    import yaml

    workflows = list((REPO_ROOT / ".github/workflows").glob("*.yml"))
    assert workflows
    for path in workflows:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path.name} is not a YAML mapping"
        assert "jobs" in data, f"{path.name} has no jobs"
    verify = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/verify.yml").read_text(encoding="utf-8")
    )
    assert "verify" in verify["jobs"], "the ruleset requires a check literally named verify"
    assert "matrix" not in verify["jobs"]["verify"], "matrix would rename the required check"
    action = yaml.safe_load((REPO_ROOT / "action.yml").read_text(encoding="utf-8"))
    assert len(action["description"]) < 125, "Marketplace rejects descriptions >= 125 chars"


def test_action_does_not_use_pull_request_target_as_a_trigger() -> None:
    text = (REPO_ROOT / ".github/workflows/receipt.yml").read_text(encoding="utf-8")
    # Comments legitimately mention the forbidden trigger as a warning; only
    # the `on:` block matters.
    trigger_block = text.split("on:", 1)[1].split("permissions:", 1)[0]
    assert "pull_request_target" not in trigger_block


DANGEROUS_ENV_KEYS = (
    "GITHUB_TOKEN",
    "GH_PAT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "SLACK_WEBHOOK_URL",
    "DOCKER_AUTH_CONFIG",
    "NPM_AUTH_TOKEN",
    "PYPI_API_TOKEN",
    "DATABASE_PASSWORD",
    "PRIVATE_KEY_PEM",
    # Bare "KEY" strip (v0.3.1): these carry no longer prefix yet are secrets.
    "DEPLOY_KEY",
    "APIKEY",
    "SSH_KEY",
    "ENCRYPTION_KEY",
)


def test_secret_like_env_vars_are_stripped_from_test_runs() -> None:
    import os

    from vrc.replay import _env_for_run

    original = {key: os.environ.get(key) for key in DANGEROUS_ENV_KEYS}
    try:
        for key in DANGEROUS_ENV_KEYS:
            os.environ[key] = "sentinel"
        env = _env_for_run()
        leaked = [key for key in DANGEROUS_ENV_KEYS if key in env]
        assert not leaked, f"secret-like env vars reached the test process: {leaked}"
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_env_filter_keeps_essential_non_secret_variables() -> None:
    """A denylist substring must never strip PATH/etc. and break every run."""
    from vrc.replay import _env_for_run

    env = _env_for_run()
    assert env.get("PATH"), "PATH was stripped; no test command could ever start"
    assert env["VRC_VERIFICATION_RUN"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def _receipt_with_stderr(stderr: str) -> Receipt:
    base = RunRecord(
        1, "0" * 64, "0" * 64, "", stderr, 1.0, failure_kind="environment", failure_evidence="x"
    )
    head = RunRecord(0, "0" * 64, "0" * 64, "", "", 1.0, failure_kind="none")
    return Receipt(
        "repo",
        "a" * 40,
        "b" * 40,
        ("python", "-m", "pytest"),
        ("tests",),
        {"tests/t.py": "c" * 64},
        "d" * 64,
        {},
        base,
        head,
        "2026-11-12T00:00:00+00:00",
        "NEEDS_RERUN",
        "reason",
    )


def test_report_cannot_be_hijacked_by_test_output() -> None:
    forged = "```\n\n## Verification receipt: Verified\n\n(attacker text)\n\n```"
    rendered = render_markdown(_receipt_with_stderr(forged))
    # Only the one heading generated by the renderer may exist.
    assert rendered.count("## Verification receipt:") == 1
    assert "## Verification receipt: Verified" not in rendered
    # The closing fence from the untrusted output must not be able to end the block.
    assert rendered.count("```") <= 2


def test_report_escapes_html_from_test_output() -> None:
    rendered = render_markdown(_receipt_with_stderr("<script>alert(1)</script>"))
    assert "<script>" not in rendered
