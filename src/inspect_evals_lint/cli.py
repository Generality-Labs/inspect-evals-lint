"""Command-line entry point.

Exit codes: 0 all checks passed, 1 some check failed, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from inspect_evals_lint import __version__
from inspect_evals_lint.config import (
    PRESETS,
    ConfigError,
    find_repo_root,
    load_config,
    read_tool_table,
)
from inspect_evals_lint.output import (
    console,
    print_check_summary,
    print_final_summary,
    print_overall_summary,
    print_report,
)
from inspect_evals_lint.runner import get_all_check_names, get_all_eval_names, lint_evaluation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect-evals-lint",
        description="Lint Inspect AI evaluations for structure, tests, best practices and sandbox pinning.",
    )
    parser.add_argument("eval_name", nargs="?", help="Name of the evaluation to lint (e.g. 'gpqa')")
    parser.add_argument(
        "--all-evals", action="store_true", help="Lint every evaluation in the repository"
    )
    parser.add_argument(
        "--check",
        metavar="CHECK_NAME",
        help="Run only this check (see --list-checks)",
    )
    parser.add_argument("--list-checks", action="store_true", help="List available checks and exit")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print the summary (useful with --all-evals)",
    )
    parser.add_argument(
        "--check-summary",
        action="store_true",
        help="Per-check compliance across all evals (implies --all-evals --summary-only)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Repository root (default: nearest pyproject.toml with [tool.inspect-evals-lint], else cwd)",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="Layout preset, overriding the pyproject 'preset' key",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_checks:
        print("Available checks:")
        for check_name in get_all_check_names():
            print(f"  {check_name}")
        sys.exit(0)

    if args.check and args.check not in get_all_check_names():
        print(f"Error: Unknown check '{args.check}'", file=sys.stderr)
        print(f"Available checks: {', '.join(get_all_check_names())}", file=sys.stderr)
        sys.exit(2)

    if args.check_summary:
        args.all_evals = True
        args.summary_only = True

    if not args.eval_name and not args.all_evals:
        parser.error("Either provide an eval_name or use --all-evals")

    repo_root = (args.root or find_repo_root()).resolve()
    try:
        if read_tool_table(repo_root) is None and args.preset is None:
            console.print(
                f"[dim]No [tool.inspect-evals-lint] table in {repo_root / 'pyproject.toml'}; "
                "using the 'template' preset.[/]"
            )
        config = load_config(repo_root, preset=args.preset)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.all_evals:
        eval_names = get_all_eval_names(repo_root, config)
        check_msg = f" (check: {args.check})" if args.check else ""
        print(f"Linting {len(eval_names)} evaluations{check_msg}...\n")

        reports = [
            lint_evaluation(repo_root, name, config, check=args.check) for name in eval_names
        ]
        if not args.summary_only:
            for report in reports:
                print_report(report, config)

        if args.check_summary:
            print_check_summary(reports)
        else:
            print_overall_summary(reports)
        print_final_summary(reports)

        sys.exit(0 if all(r.passed() for r in reports) else 1)

    report = lint_evaluation(repo_root, args.eval_name, config, check=args.check)
    print_report(report, config)
    sys.exit(0 if report.passed() else 1)


if __name__ == "__main__":
    main()
