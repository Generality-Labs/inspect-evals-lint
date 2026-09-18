"""Command-line entry point.

Exit codes: 0 all checks passed, 1 some check failed, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from rich.markup import escape
from rich.table import Table

from inspect_evals_lint import __version__
from inspect_evals_lint.config import (
    PRESETS,
    ConfigError,
    LintConfig,
    find_repo_root,
    known_selectors,
    load_config,
    read_tool_table,
)
from inspect_evals_lint.context import evaluation_names, helper_names
from inspect_evals_lint.registry import Rule, get_rule, rules
from inspect_evals_lint.render import (
    print_check_summary,
    print_final_summary,
    print_overall_summary,
    print_report,
    render_github,
    render_json,
)
from inspect_evals_lint.render.console import console, stderr_console
from inspect_evals_lint.runner import lint_repository

OUTPUT_FORMATS = ("text", "json", "github")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect-evals-lint",
        description="Lint Inspect AI evaluations for structure, tests, best practices and sandbox pinning.",
    )
    parser.add_argument(
        "packages",
        nargs="*",
        metavar="PACKAGE",
        help="Evaluation or helper packages to lint (e.g. gpqa, utils); default: every package with --all",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Lint every evaluation and helper package in the repository",
    )
    parser.add_argument(
        "--select",
        metavar="RULES",
        help="Only run these rules: names, codes or code prefixes, comma-separated (overrides the config)",
    )
    parser.add_argument(
        "--ignore",
        metavar="RULES",
        help="Also skip these rules: names, codes or code prefixes, comma-separated",
    )
    parser.add_argument("--list-rules", action="store_true", help="List every rule and exit")
    parser.add_argument(
        "--explain", metavar="RULE", help="Print a rule's documentation (by code or name) and exit"
    )
    parser.add_argument(
        "--output-format",
        choices=OUTPUT_FORMATS,
        default="text",
        help=(
            "text (default) prints reports and summaries; json writes one document to stdout; "
            "github writes one workflow annotation per finding"
        ),
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


def _fail(message: str, code: int = 2) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)


def _rule_dict(rule: Rule) -> dict[str, object]:
    return {
        "code": rule.code,
        "name": rule.name,
        "category": rule.category,
        "scopes": sorted(rule.scopes),
        "summary": rule.summary,
        "allowlist": rule.allowlist,
    }


def _list_rules(output_format: str) -> None:
    if output_format == "json":
        sys.stdout.write(json.dumps([_rule_dict(r) for r in rules()], indent=2) + "\n")
        return
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Code", style="cyan", no_wrap=True)
    table.add_column("Rule", style="cyan", no_wrap=True)
    table.add_column("Category", no_wrap=True)
    table.add_column("Scope", no_wrap=True)
    table.add_column("Summary", overflow="fold")
    for rule in rules():
        table.add_row(
            rule.code,
            rule.name,
            rule.category,
            "+".join(sorted(rule.scopes)),
            escape(rule.summary),
        )
    console.print(table)


def _explain(rule: Rule, output_format: str) -> None:
    """Print the rule's page: the same text as ``docs/rules/<code>.md``, rendered for the terminal."""
    from rich.markdown import Markdown

    from inspect_evals_lint.docs import GENERATED_NOTE, formatted, rule_page

    page = formatted(rule_page(rule)).replace(GENERATED_NOTE, "").lstrip()
    if output_format == "json":
        sys.stdout.write(json.dumps({**_rule_dict(rule), "doc": page}, indent=2) + "\n")
        return
    console.print(Markdown(page))


def _selectors(raw: str | None, key: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        _fail(f"--{key} needs at least one rule")
    return known_selectors(values, key)


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_rules:
        _list_rules(args.output_format)
        sys.exit(0)

    if args.explain:
        rule = get_rule(args.explain)
        if rule is None:
            _fail(f"Unknown rule '{args.explain}'. Use --list-rules to see them.")
            return
        _explain(rule, args.output_format)
        sys.exit(0)

    if not args.packages and not args.all:
        parser.error("Name at least one package or use --all")

    # Under a machine-readable format stdout carries only the document; everything informational goes to stderr.
    info = stderr_console if args.output_format != "text" else console

    repo_root = (args.root or find_repo_root()).resolve()
    try:
        if read_tool_table(repo_root) is None and args.preset is None:
            info.print(
                f"[dim]No {escape('[tool.inspect-evals-lint]')} table in "
                f"{repo_root / 'pyproject.toml'}; using the 'template' preset.[/]",
                soft_wrap=True,
            )
        config = load_config(repo_root, preset=args.preset)
        config = _with_cli_selection(config, args.select, args.ignore)
    except ConfigError as e:
        _fail(str(e))
        return

    if args.all:
        evals = evaluation_names(repo_root, config)
        helpers = helper_names(repo_root, config)
        what = f"{len(evals)} evaluations"
        if helpers:
            plural = "s" if len(helpers) != 1 else ""
            what += f" and {len(helpers)} helper package{plural}"
        info.print(f"Linting {what}...\n", markup=False, soft_wrap=True)
        names = [
            *evals,
            *helpers,
            *[p for p in args.packages if p not in evals and p not in helpers],
        ]
    else:
        names = list(dict.fromkeys(args.packages))

    try:
        run = lint_repository(repo_root, config, names=names)
    except ConfigError as e:
        _fail(str(e))
        return

    if args.output_format == "json":
        sys.stdout.write(render_json(run))
        sys.exit(0 if run.passed() else 1)
    if args.output_format == "github":
        sys.stdout.write(render_github(run))
        sys.exit(0 if run.passed() else 1)

    for report in run.packages:
        print_report(report, config, root=repo_root)
    if len(run.packages) > 1:
        print_overall_summary(run)
        print_check_summary(run)
        print_final_summary(run)
    sys.exit(0 if run.passed() else 1)


def _with_cli_selection(config: LintConfig, select: str | None, ignore: str | None) -> LintConfig:
    """Apply ``--select`` (replacing the configured selection) and ``--ignore`` (adding to it)."""
    from dataclasses import replace

    selected = _selectors(select, "select")
    ignored = _selectors(ignore, "ignore")
    if selected is not None:
        config = replace(config, select=selected)
    if ignored is not None:
        config = replace(config, ignore=(*config.ignore, *ignored))
    return config


if __name__ == "__main__":
    main()
