"""Documentation consistency contracts enforced in CI."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from vrc import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_action_version_references_match_the_package_version() -> None:
    """README and docs must never point users at a stale action tag."""
    files = (REPO_ROOT / "README.md", REPO_ROOT / "docs/runners.md")
    refs: list[str] = []
    for path in files:
        refs += re.findall(
            r"verifiable-contributions@v([0-9]+\.[0-9]+\.[0-9]+)", path.read_text(encoding="utf-8")
        )
    assert refs, "expected documented @version usage examples"
    assert set(refs) == {__version__}, f"docs reference {set(refs)}, package is {__version__}"


def test_python_floor_is_consistent_everywhere() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["project"]["requires-python"]
    assert requires == ">=3.11"
    assert pyproject["tool"]["ruff"]["target-version"] == "py311"
    verify = (REPO_ROOT / ".github/workflows/verify.yml").read_text(encoding="utf-8")
    compat = (REPO_ROOT / ".github/workflows/compat.yml").read_text(encoding="utf-8")
    # `verify` must stay a single job so its check-run name stays exactly
    # "verify" (the main ruleset requires that name).
    assert "matrix:" not in verify
    for version in ("3.11", "3.12", "3.13"):
        assert f'"{version}"' in compat, f"compat workflow must test Python {version}"
    for contract in (
        "test_policy_contract.py",
        "test_release_contract.py",
        "test_docs_contract.py",
        "test_no_false_positive.py",
    ):
        assert contract in compat, f"compat workflow must run {contract}"


def test_threat_model_describes_the_current_controls() -> None:
    text = (REPO_ROOT / "docs/threat-model.md").read_text(encoding="utf-8")
    assert "workflows/" in text, "threat model must describe the workflows-directory protection"
    assert "tests/" in text, "threat model must document the tests/ trade-off"
    assert "vrc check" in text, "threat model must mention the action self-check"


def test_readme_documents_the_false_positive_guard() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "test_paths" in text
    assert "negative" in text.lower() or "never" in text.lower()
