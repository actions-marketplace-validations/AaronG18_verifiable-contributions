"""Load the verification policy from the base ref only."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .classification import RUNNER_ASSERTION_MARKERS, markers_for

# `pytest` needs no preset: its signatures are the built-in defaults.
KNOWN_RUNNERS: frozenset[str] = frozenset({*RUNNER_ASSERTION_MARKERS, "pytest"})

CONFIG_NAME = ".vrc.toml"

DEFAULT_COMMAND: tuple[str, ...] = ("python", "-m", "pytest", "-q")
DEFAULT_TEST_PATHS: tuple[str, ...] = ("tests", "test")

# Every key a policy may set. Unknown keys are rejected instead of silently
# ignored: a misspelled key (or putting an action input such as `setup-command`
# in the policy) must not make the user think a control is active when it is
# not. `setup-command` belongs in the workflow's `with:` block, not here.
KNOWN_POLICY_KEYS: frozenset[str] = frozenset(
    {
        "command",
        "command_allowlist",
        "test_paths",
        "protected_paths",
        "timeout_seconds",
        "runner",
        "assertion_markers",
        "environment_markers",
        "working_directory",
    }
)

# Executables accepted in the test command. The policy is read from the base
# ref, so a contributor cannot add an executable in the PR being verified.
# Projects with their own launcher can extend this via `command_allowlist`.
ALLOWLISTED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "uv",
        "tox",
        "npm",
        "npx",
        "node",
        "pnpm",
        "yarn",
        "bun",
        "go",
    }
)

# A test path rooted at one of these would copy the source change onto the base
# tree, defeating the entire check. Glob patterns (e.g. "**/*_test.go") are
# allowed because they select test files explicitly.
UNSAFE_TEST_PATH_ROOTS: frozenset[str] = frozenset(
    {".", "", "/", "src", "lib", "app", "pkg", "internal", "packages"}
)

# Protect the whole workflows directory, not just receipt.yml: verify.yml and
# open-pr.yml also control what runs in CI. `tests/` is deliberately NOT here —
# this repository dogfoods the action, and protecting tests would make every
# one of its own PRs UNTRUSTED, destroying that signal. See the threat model.
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "vrc/",
    "action.yml",
    ".vrc.toml",
    ".github/workflows/",
    ".github/dependabot.yml",
    "SECURITY.md",
    "pyproject.toml",
)


class ConfigError(ValueError):
    """Raised when the verification policy is missing or malformed."""


@dataclass(frozen=True)
class Config:
    command: tuple[str, ...]
    test_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    timeout_seconds: int
    runner: tuple[str, ...]
    environment_markers: tuple[str, ...]
    assertion_markers: tuple[str, ...]
    # Directory (relative to the extracted repository root) the test command
    # runs in. Needed for monorepos, where the package and its tests live under
    # a subpath such as `libs/langgraph`. Empty means the repository root.
    working_directory: str = ""

    @classmethod
    def default(cls) -> Config:
        environment_markers, assertion_markers = markers_for()
        return cls(
            command=DEFAULT_COMMAND,
            test_paths=DEFAULT_TEST_PATHS,
            protected_paths=DEFAULT_PROTECTED_PATHS,
            timeout_seconds=300,
            runner=(),
            environment_markers=environment_markers,
            assertion_markers=assertion_markers,
        )

    @classmethod
    def load(cls, source: Path | bytes) -> Config:
        if isinstance(source, bytes):
            raw = source
        else:
            if not source.is_file():
                raise ConfigError(f"policy file not found: {source}")
            raw = source.read_bytes()
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"invalid {CONFIG_NAME}: {exc}") from exc

        unknown = sorted(set(data) - KNOWN_POLICY_KEYS)
        if unknown:
            hint = ""
            if "setup-command" in unknown:
                hint = (
                    " `setup-command` is an input of the action's `with:` block in the "
                    "workflow, not a policy key; install dependencies there (or locally "
                    "before running `vrc replay`)."
                )
            raise ConfigError(
                f"unknown policy key(s) {unknown!r}; valid keys are "
                f"{sorted(KNOWN_POLICY_KEYS)}.{hint}"
            )

        workdir = data.get("working_directory", "")
        if not isinstance(workdir, str):
            raise ConfigError("working_directory must be a string")
        # Check the RAW string: PurePosixPath silently normalises "a//b" and
        # "a/./b", which would let an ambiguous path through. A single trailing
        # slash is harmless, so strip exactly that before splitting.
        normalized = workdir[:-1] if workdir.endswith("/") else workdir
        if normalized:
            parts = normalized.split("/")
            if (
                PurePosixPath(normalized).is_absolute()
                or normalized.startswith("/")
                or "\\" in normalized
                or any(part in ("", ".", "..") for part in parts)
            ):
                raise ConfigError(
                    "working_directory must be a safe relative path inside the "
                    "repository (no '..', '.', absolute paths, backslashes or empty segments)"
                )
        workdir = workdir.strip("/")

        command = data.get("command", list(DEFAULT_COMMAND))
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ConfigError("command must be a non-empty list of non-empty strings")
        executable = Path(command[0]).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
        allowlisted = set(ALLOWLISTED_EXECUTABLES)
        custom = data.get("command_allowlist", [])
        if not isinstance(custom, list) or not all(
            isinstance(item, str) and item.strip() for item in custom
        ):
            raise ConfigError("command_allowlist must be a list of non-empty strings")
        allowlisted.update(item.strip().lower() for item in custom)
        if executable not in allowlisted and not executable.startswith("python3."):
            raise ConfigError(
                f"command executable {command[0]!r} is not allowlisted; "
                "add it to command_allowlist in the base policy if this is intentional"
            )

        paths = data.get("test_paths", list(DEFAULT_TEST_PATHS))
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p and ".." not in p for p in paths)
        ):
            raise ConfigError("test_paths must be a non-empty list of safe relative paths")
        for path in paths:
            first = path.strip("/").split("/")[0].lower()
            if any(char in path for char in "*?["):
                continue  # explicit glob, e.g. "**/*_test.go"
            if first in UNSAFE_TEST_PATH_ROOTS:
                raise ConfigError(
                    f"test_paths entry {path!r} would copy the source change onto the "
                    "base tree; use a test-only directory or a glob such as '**/*_test.go'"
                )

        protected = data.get("protected_paths", list(DEFAULT_PROTECTED_PATHS))
        if not isinstance(protected, list) or not all(isinstance(p, str) for p in protected):
            raise ConfigError("protected_paths must be a list of strings")

        timeout = data.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 10 <= timeout <= 3600:
            raise ConfigError("timeout_seconds must be an integer between 10 and 3600")

        runners_raw = data.get("runner", [])
        if not isinstance(runners_raw, list) or not all(
            isinstance(r, str) and r.strip() for r in runners_raw
        ):
            raise ConfigError("runner must be a list of non-empty strings")
        # A typo like "go_test" used to be silently ignored: the check still
        # failed closed, but the user could not tell why. Reject it explicitly.
        runners = tuple(r.strip().lower() for r in runners_raw)
        unknown = [r for r in runners if r not in KNOWN_RUNNERS]
        if unknown:
            raise ConfigError(
                f"unknown runner(s) {unknown!r}; valid values are "
                f"{sorted(KNOWN_RUNNERS)}. Use assertion_markers for a custom runner."
            )

        def extra_markers(key: str) -> tuple[str, ...]:
            raw_markers = data.get(key, [])
            if not isinstance(raw_markers, list) or not all(
                isinstance(m, str) and m.strip() for m in raw_markers
            ):
                raise ConfigError(f"{key} must be a list of non-empty strings")
            # Custom markers are appended, never replacing the built-in ones, so
            # a policy cannot switch off the environment-error detection.
            return tuple(m.strip().lower() for m in raw_markers)

        environment_markers, assertion_markers = markers_for(
            runners,
            extra_environment=extra_markers("environment_markers"),
            extra_assertion=extra_markers("assertion_markers"),
        )

        return cls(
            command=tuple(command),
            test_paths=tuple(p.rstrip("/") for p in paths),
            protected_paths=tuple(protected),
            timeout_seconds=timeout,
            runner=runners,
            environment_markers=environment_markers,
            assertion_markers=assertion_markers,
            working_directory=workdir,
        )
