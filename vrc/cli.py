"""Command line interface for vrc."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__
from .audit import (
    AuditError,
    audit,
    check_audit,
    render_audit_markdown,
)
from .config import Config, ConfigError
from .receipt import Receipt, check_receipt
from .replay import ReplayError, replay
from .report import render_markdown

VERDICTS = ("VERIFIED", "NEEDS_EVIDENCE", "NEEDS_RERUN", "UNTRUSTED")


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read receipt {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("receipt must be a JSON object")
    return data


def _cmd_replay(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    with tempfile.TemporaryDirectory(prefix="vrc-") as tmp:
        outcome = replay(
            repo,
            args.base,
            args.head,
            workdir=Path(tmp),
            repository=args.repository,
        )
        receipt = outcome.receipt
        text = receipt.to_json()
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        if args.format == "markdown":
            sys.stdout.write(render_markdown(receipt))
        elif args.format == "summary":
            print(
                json.dumps(
                    {
                        "verdict": receipt.verdict,
                        "reason": receipt.reason,
                        "base_exit_code": receipt.base_run.exit_code,
                        "head_exit_code": receipt.head_run.exit_code,
                        "test_files": len(receipt.test_files_sha256),
                    },
                    indent=2,
                )
            )
        else:
            sys.stdout.write(text)
    print(f"vrc: {receipt.verdict} - {receipt.reason}", file=sys.stderr)
    for verdict in args.fail_on.split(","):
        if verdict.strip() and verdict.strip() == receipt.verdict:
            return 1
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        data = _load_receipt(Path(args.receipt))
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    ok, problems = check_receipt(data)
    if ok:
        print(f"OK: receipt is internally consistent (verdict={data.get('verdict')})")
        return 0
    for problem in problems:
        print(f"INVALID: {problem}", file=sys.stderr)
    return 1


def _cmd_explain(args: argparse.Namespace) -> int:
    try:
        receipt = Receipt.from_dict(_load_receipt(Path(args.receipt)))
    except (ValueError, KeyError, TypeError) as exc:
        print(f"cannot render receipt: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(render_markdown(receipt))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """Check a policy locally, without CI, PRs or a maintainer's approval.

    A workflow from a fork does not run until a maintainer approves it, so a
    broken policy used to be discovered only after someone else had spent time
    on it. `vrc validate` catches that before the PR is opened.
    """
    policy = Path(args.policy)
    try:
        config = Config.load(policy)
    except ConfigError as exc:
        print(f"INVALID policy: {exc}", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    problems: list[str] = []
    if config.working_directory and not (repo / config.working_directory).is_dir():
        problems.append(
            f"working_directory {config.working_directory!r} does not exist under {repo}"
        )
    for raw in config.test_paths:
        target = repo / raw
        if any(char in raw for char in "*?["):
            if not any(repo.glob(raw)):
                problems.append(f"test path {raw!r} matches no files under {repo}")
        elif not target.exists():
            problems.append(f"test path {raw!r} does not exist under {repo}")

    if problems:
        for problem in problems:
            print(f"INVALID policy: {problem}", file=sys.stderr)
        return 1

    print(f"OK: {policy} is a valid, usable policy for {repo}")
    print(f"  command: {' '.join(config.command)}")
    print(f"  working directory: {config.working_directory or '(repository root)'}")
    print(f"  test paths: {', '.join(config.test_paths)}")
    print(f"  runners: {', '.join(config.runner) or '(built-in defaults)'}")
    print(f"  timeout: {config.timeout_seconds}s")
    print(
        "  note: a PR that adds .vrc.toml or the verify workflow will be UNTRUSTED; "
        "land the wiring in its own PR first."
    )
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    with tempfile.TemporaryDirectory(prefix="vrc-audit-") as tmp:
        report = audit(
            repo,
            args.base,
            args.head,
            workdir=Path(tmp),
            max_points=args.max_points,
            repository=args.repository,
            only_point=args.point,
        )
        text = report.to_json()
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        if args.format == "markdown":
            sys.stdout.write(render_audit_markdown(report))
        elif args.format == "summary":
            print(
                json.dumps(
                    {
                        "verdict": report.verdict,
                        "points": len(report.points),
                        "unaudited_points": report.unaudited_points,
                        "pinned": sum(1 for p in report.points if p.status == "PINNED"),
                        "unpinned": sum(1 for p in report.points if p.status == "UNPINNED"),
                        "inconclusive": sum(1 for p in report.points if p.status == "INCONCLUSIVE"),
                        "tests_passed": report.tests_passed,
                    },
                    indent=2,
                )
            )
        else:
            sys.stdout.write(text)
    print(f"vrc audit: {report.verdict}", file=sys.stderr)
    for verdict in args.fail_on.split(","):
        if verdict.strip() and verdict.strip() == report.verdict:
            return 1
    return 0


def _cmd_audit_check(args: argparse.Namespace) -> int:
    try:
        data = _load_receipt(Path(args.report))
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    ok, problems = check_audit(data)
    if ok:
        print(f"OK: audit report is internally consistent (verdict={data.get('verdict')})")
        return 0
    for problem in problems:
        print(f"INVALID: {problem}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vrc",
        description="Independently replay the tests attached to a change proposal.",
    )
    parser.add_argument("--version", action="version", version=f"vrc {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_replay = sub.add_parser("replay", help="replay tests on base and head trees")
    p_replay.add_argument("--repo", default=".", help="path to the git repository")
    p_replay.add_argument("--base", required=True, help="base ref (before the change)")
    p_replay.add_argument("--head", required=True, help="head ref (with the change)")
    p_replay.add_argument(
        "--repository", default=None, help="repository identifier for the receipt"
    )
    p_replay.add_argument("--output", default=None, help="write the receipt JSON here")
    p_replay.add_argument("--format", choices=("json", "summary", "markdown"), default="json")
    p_replay.add_argument(
        "--fail-on",
        default="",
        help="comma-separated verdicts that should produce a non-zero exit code, "
        "e.g. NEEDS_EVIDENCE,NEEDS_RERUN. Empty means report-only.",
    )
    p_replay.set_defaults(func=_cmd_replay)

    p_check = sub.add_parser("check", help="validate a receipt's internal consistency")
    p_check.add_argument("receipt")
    p_check.set_defaults(func=_cmd_check)

    p_explain = sub.add_parser("explain", help="render a receipt as Markdown")
    p_explain.add_argument("receipt")
    p_explain.set_defaults(func=_cmd_explain)

    p_validate = sub.add_parser(
        "validate", help="check a policy locally before opening a pull request"
    )
    p_validate.add_argument("policy", nargs="?", default=".vrc.toml", help="path to .vrc.toml")
    p_validate.add_argument(
        "--repo", default=".", help="repository root used to check working_directory and test_paths"
    )
    p_validate.set_defaults(func=_cmd_validate)

    p_audit = sub.add_parser("audit", help="audit which individual change points the tests observe")
    p_audit.add_argument("--repo", default=".", help="path to the git repository")
    p_audit.add_argument("--base", required=True, help="base ref (before the change)")
    p_audit.add_argument("--head", required=True, help="head ref (with the change)")
    p_audit.add_argument("--repository", default=None, help="repository identifier")
    p_audit.add_argument("--output", default=None, help="write the audit report JSON here")
    p_audit.add_argument("--format", choices=("json", "summary", "markdown"), default="json")
    p_audit.add_argument(
        "--point",
        type=int,
        default=None,
        help="audit only this change point (1-based), to reproduce one claim",
    )
    p_audit.add_argument(
        "--max-points",
        type=int,
        default=20,
        help="maximum number of change points to audit (default 20)",
    )
    p_audit.add_argument(
        "--fail-on",
        default="",
        help="comma-separated audit verdicts that produce a non-zero exit code",
    )
    p_audit.set_defaults(func=_cmd_audit)

    p_audit_check = sub.add_parser("audit-check", help="validate an audit report's integrity")
    p_audit_check.add_argument("report")
    p_audit_check.set_defaults(func=_cmd_audit_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ReplayError, ConfigError, AuditError) as exc:
        print(f"vrc: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
