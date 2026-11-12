"""Policy/documentation contracts that CI must keep honest."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.helpers import BUGGY_MODULE, CATCHING_TEST, FIXED_MODULE, VACUOUS_TEST, git
from vrc.config import Config, ConfigError
from vrc.replay import _iter_files, _overlay_test_paths, replay

REPO_ROOT = Path(__file__).resolve().parent.parent


def _toml_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return re.findall(r"```toml\s*\n(.*?)```", text, flags=re.DOTALL)


def test_every_documented_policy_is_loadable() -> None:
    """docs/runners.md and README examples must be executable, not aspirational."""
    blocks = _toml_blocks(REPO_ROOT / "docs/runners.md") + _toml_blocks(REPO_ROOT / "README.md")
    assert len(blocks) >= 6, "expected several documented policy examples"
    for block in blocks:
        parsed = tomllib.loads(block)
        config = Config.load(block.encode("utf-8"))
        assert config.command, parsed
        assert config.test_paths, parsed


def test_documented_test_paths_never_copy_the_source_tree() -> None:
    for block in _toml_blocks(REPO_ROOT / "docs/runners.md"):
        paths = tomllib.loads(block).get("test_paths", [])
        for path in paths:
            first = path.strip("/").split("/")[0].lower()
            assert first not in {".", "", "src", "lib", "app", "pkg", "internal"}, (
                f"documented test path {path!r} would copy source files onto the base tree"
            )
        assert paths, "every example must declare test_paths"


def test_common_runner_launchers_are_allowlisted() -> None:
    for exe in ("npx", "node", "yarn", "bun", "npm", "go", "python", "pytest"):
        config = Config.load(f'command = ["{exe}", "test"]\n'.encode())
        assert config.command[0] == exe


def test_custom_runner_requires_explicit_allowlist_extension() -> None:
    with pytest.raises(ConfigError, match="not allowlisted"):
        Config.load(b'command = ["my-runner", "test"]\n')
    config = Config.load(b'command = ["my-runner", "test"]\ncommand_allowlist = ["my-runner"]\n')
    assert config.command[0] == "my-runner"


def test_command_allowlist_must_be_a_list_of_strings() -> None:
    with pytest.raises(ConfigError, match="command_allowlist"):
        Config.load(b'command = ["python"]\ncommand_allowlist = "oops"\n')


def test_root_and_source_test_paths_are_rejected() -> None:
    for path in (".", "", "src/", "lib/", "internal/"):
        with pytest.raises(ConfigError, match="test_paths"):
            Config.load(f'command = ["python"]\ntest_paths = ["{path}"]\n'.encode())


def test_glob_policy_is_loadable() -> None:
    config = Config.load(b'command = ["go", "test", "./..."]\ntest_paths = ["**/*_test.go"]\n')
    assert config.test_paths == ("**/*_test.go",)


def test_glob_iter_files_selects_only_matching_files(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.go").write_text("package pkg\n", encoding="utf-8")
    (tmp_path / "pkg" / "calc_test.go").write_text("package pkg\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.test.ts").write_text("test()", encoding="utf-8")
    (tmp_path / "src" / "app.ts").write_text("code", encoding="utf-8")

    go = _iter_files(tmp_path, Path("**/*_test.go"))
    assert [p.as_posix() for p in go] == ["pkg/calc_test.go"]
    ts = _iter_files(tmp_path, Path("src/**/*.test.ts"))
    assert [p.as_posix() for p in ts] == ["src/app.test.ts"]


def test_glob_overlay_leaves_source_untouched(tmp_path: Path) -> None:
    base, head = tmp_path / "base", tmp_path / "head"
    for root in (base, head):
        (root / "pkg").mkdir(parents=True)
    (base / "pkg" / "calc.go").write_text("OLD SOURCE", encoding="utf-8")
    (head / "pkg" / "calc.go").write_text("NEW SOURCE", encoding="utf-8")
    (base / "pkg" / "calc_test.go").write_text("old test", encoding="utf-8")
    (head / "pkg" / "calc_test.go").write_text("new test", encoding="utf-8")

    digests = _overlay_test_paths(base, head, ("**/*_test.go",))

    assert list(digests) == ["pkg/calc_test.go"]
    assert (base / "pkg" / "calc.go").read_text(encoding="utf-8") == "OLD SOURCE"
    assert (base / "pkg" / "calc_test.go").read_text(encoding="utf-8") == "new test"


def test_end_to_end_glob_policy_does_not_leak_the_fix(tmp_path: Path, python_command) -> None:
    """A glob test path must still yield VERIFIED with the base source intact."""
    import json

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "vrc@example.invalid")
    git(repo, "config", "user.name", "vrc test")

    policy = (
        f"command = {json.dumps(python_command)}\n"
        'test_paths = ["tests/test_*.py"]\n'
        "timeout_seconds = 120\n"
    )

    (repo / "sample.py").write_text(BUGGY_MODULE, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text(VACUOUS_TEST, encoding="utf-8")
    (repo / ".vrc.toml").write_text(policy, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base_sha = git(repo, "rev-parse", "HEAD").strip()

    (repo / "sample.py").write_text(FIXED_MODULE, encoding="utf-8")
    (repo / "tests" / "test_sample.py").write_text(CATCHING_TEST, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    head_sha = git(repo, "rev-parse", "HEAD").strip()

    outcome = replay(repo, base_sha, head_sha, workdir=tmp_path / "work")

    assert outcome.receipt.verdict == "VERIFIED"
    assert list(outcome.receipt.test_files_sha256) == ["tests/test_sample.py"]
    assert (outcome.base_dir / "sample.py").read_text(encoding="utf-8") == BUGGY_MODULE
    assert (outcome.head_dir / "sample.py").read_text(encoding="utf-8") == FIXED_MODULE
