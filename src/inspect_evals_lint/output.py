"""Rich console rendering of run reports, and the JSON document."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from inspect_evals_lint import __version__
from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.diagnostics import (
    STATUSES,
    Diagnostic,
    Finding,
    Outcome,
    PackageReport,
    RunReport,
    Status,
)

console = Console()
stderr_console = Console(stderr=True)

CHECKS_DOC_URL = "https://github.com/Generality-Labs/inspect-evals-lint/blob/main/docs/CHECKS.md"

SCHEMA_VERSION = 1
"""Bumped when the JSON document changes shape."""

_STATUS_MARKUP = {
    "pass": "[bold green]PASS[/]",
    "fail": "[bold red]FAIL[/]",
    "warn": "[bold yellow]WARN[/]",
    "skip": "[dim]SKIP[/]",
    "suppressed": "[bold blue]SUPP[/]",
}
_MESSAGE_STYLE = {"pass": "", "fail": "red", "warn": "yellow", "skip": "dim", "suppressed": "blue"}
_SUMMARY_LABELS: tuple[tuple[Status, str, str], ...] = (
    ("pass", "passed", "green"),
    ("fail", "failed", "red"),
    ("warn", "warnings", "yellow"),
    ("skip", "skipped", "dim"),
    ("suppressed", "suppressed", "blue"),
)


def get_status_text(status: str) -> Text:
    return Text.from_markup(_STATUS_MARKUP.get(status, status.upper()))


def _label(report: PackageReport) -> str:
    """The package name, marked when it is a helper rather than an evaluation."""
    return f"{report.name} (helper)" if report.kind == "helper" else report.name


def _rule_cell(item: Finding) -> str:
    if item.rule is None:
        return "?"
    return f"{item.rule.code} {item.rule.name}"


def _relative(path: Path, root: Path | None) -> str:
    if root is not None and path.is_absolute() and path.is_relative_to(root):
        return path.relative_to(root).as_posix()
    return str(path)


def _location(item: Finding, root: Path | None) -> str:
    if not isinstance(item, Diagnostic):
        return ""
    text = _relative(item.file, root)
    if item.line is not None:
        text += f":{item.line}"
        if item.column is not None:
            text += f":{item.column}"
    return text


def _message(item: Finding) -> str:
    if isinstance(item, Diagnostic):
        if item.suppressed:
            return f"[suppressed] {item.message}"
        return item.message if item.hint is None else f"{item.message}; {item.hint}"
    return item.message


def print_report(
    report: PackageReport, config: LintConfig | None = None, root: Path | None = None
) -> None:
    """Print one package's findings with suppression hints for any failures."""
    source_root = config.source_root if config else "src"

    console.print()
    title = f"Lint Report: {report.name}"
    if report.kind == "helper":
        title += " (helper package)"
    console.print(Rule(f"[bold]{title}[/]", style="blue"))
    console.print()

    if report.skipped:
        console.print(f"[dim]Skipped: {report.skipped}[/]")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Status", width=6)
    table.add_column("Rule", style="cyan")
    table.add_column("Message", overflow="fold")
    table.add_column("Location", style="dim", overflow="fold")

    failed_rules: list[str] = []
    for item in report.items():
        table.add_row(
            get_status_text(item.status),
            _rule_cell(item),
            Text(_message(item), style=_MESSAGE_STYLE.get(item.status, "")),
            _location(item, root),
        )
        if item.status == "fail" and item.rule is not None:
            failed_rules.append(item.rule.name)
    console.print(table)

    summary = report.summary()
    console.print()
    console.print(Rule(style="dim"))
    summary_parts: list[str] = []
    for status, label, style in _SUMMARY_LABELS:
        if summary[status] > 0:
            summary_parts.append(f"[{style}]{summary[status]} {label}[/]")
    console.print(f"Summary: {', '.join(summary_parts)}")

    if report.passed():
        console.print("[bold green]All required checks passed![/]")
        return

    console.print("[bold red]Some checks failed.[/]")
    console.print()
    console.print("[dim]To suppress a finding, add one of:[/]")
    for name in dict.fromkeys(failed_rules):
        comment = escape(f"# inspect-evals-lint: ignore[{name}]")
        console.print(f"  [cyan]{comment}[/]  [dim](on the line)[/]")
    table_entry = escape(f'per-file-ignores = {{ "{source_root}/{report.name}/**" = ["<rule>"] }}')
    console.print(f"  [cyan]{table_entry}[/]  [dim](in {escape('[tool.inspect-evals-lint]')})[/]")
    console.print()
    console.print(f"[dim]More info about these checks: {CHECKS_DOC_URL}[/]")


_PASS_RATE_HIGH = 0.80
_PASS_RATE_MED = 0.50


def _pass_rate_text(rate: float) -> Text:
    pct = rate * 100
    if rate == 1.0:
        return Text("100%", style="bold green")
    if rate >= _PASS_RATE_HIGH:
        return Text(f"{pct:.0f}%", style="green")
    if rate >= _PASS_RATE_MED:
        return Text(f"{pct:.0f}%", style="yellow")
    return Text(f"{pct:.0f}%", style="red")


def print_check_summary(run: RunReport) -> None:
    """Print per-rule compliance across all packages, worst first."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(STATUSES, 0))
    for package in run.packages:
        for name, statuses in package.statuses().items():
            for status in statuses:
                counts[name][status] += 1

    console.print()
    console.print(Rule("[bold]CHECK COMPLIANCE SUMMARY[/]", style="blue"))
    console.print()

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Check", style="cyan")
    table.add_column("Pass", justify="right", style="green")
    table.add_column("Fail", justify="right", style="red")
    table.add_column("Warn", justify="right", style="yellow")
    table.add_column("Skip", justify="right", style="dim")
    table.add_column("Supp", justify="right", style="blue")
    table.add_column("% Pass", justify="right")

    sorted_checks = sorted(
        counts.items(),
        key=lambda item: item[1]["pass"] / max(item[1]["pass"] + item[1]["fail"], 1),
    )
    for name, c in sorted_checks:
        applicable = c["pass"] + c["fail"] + c["warn"]
        rate_text = (
            _pass_rate_text(c["pass"] / applicable) if applicable else Text("-", style="dim")
        )
        table.add_row(
            name,
            str(c["pass"]),
            str(c["fail"]),
            str(c["warn"]),
            str(c["skip"]),
            str(c["suppressed"]),
            rate_text,
        )
    console.print(table)
    console.print()


def print_final_summary(run: RunReport) -> None:
    """Print the rules that ran, then failures and warnings grouped by rule.

    When many packages fail the same rule the grouping shows the pattern and
    every affected package in one place.
    """
    names = sorted({name for p in run.packages for name in p.rules_run()})

    console.print()
    console.print(Rule("[bold]FINAL SUMMARY[/]", style="blue"))
    console.print()
    console.print(
        f"[bold]Checks run ({len(names)}):[/] " + ", ".join(f"[cyan]{n}[/]" for n in names)
    )
    console.print()

    if not _print_grouped(run, status="fail", noun="failure", style="red"):
        console.print("[bold green]No failures.[/]")
    _print_grouped(run, status="warn", noun="warning", style="yellow")


def _print_grouped(run: RunReport, status: Status, noun: str, style: str) -> bool:
    by_rule: dict[str, list[tuple[str, Diagnostic]]] = {}
    for package in run.packages:
        for d in package.diagnostics:
            if d.status == status and d.rule is not None:
                by_rule.setdefault(d.rule.name, []).append((_label(package), d))
    if not by_rule:
        return False

    console.print(f"[bold]{noun.capitalize()}s by check:[/]")
    for name, entries in sorted(by_rule.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        plural = noun if len(entries) == 1 else f"{noun}s"
        console.print()
        console.print(f"[cyan]{name}[/] [{style}]({len(entries)} {plural})[/]")

        table = Table(show_header=False, box=None, padding=(0, 1), pad_edge=False)
        table.add_column("Package", style="cyan", no_wrap=True)
        table.add_column("Message", style=style, overflow="fold")
        table.add_column("Location", style="dim", overflow="fold")
        for label, d in sorted(entries, key=lambda e: e[0]):
            table.add_row(f"  {label}", _message(d), _location(d, run.root))
        console.print(table)
    console.print()
    return True


def print_overall_summary(run: RunReport) -> None:
    """Print one row per package with pass/fail/warn/skip counts and totals."""
    console.print()
    console.print(Rule("[bold]OVERALL SUMMARY[/]", style="blue"))
    console.print()

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Status", width=6)
    table.add_column("Package", style="cyan")
    table.add_column("Pass", justify="right", style="green")
    table.add_column("Fail", justify="right", style="red")
    table.add_column("Warn", justify="right", style="yellow")
    table.add_column("Skip", justify="right", style="dim")

    passed = 0
    totals: dict[Status, int] = {"pass": 0, "fail": 0, "warn": 0, "skip": 0}
    for package in run.packages:
        summary = package.summary()
        for key in totals:
            totals[key] += summary[key]
        if package.passed():
            status = Text.from_markup("[bold green]PASS[/]")
            passed += 1
        else:
            status = Text.from_markup("[bold red]FAIL[/]")
        table.add_row(
            status,
            _label(package),
            str(summary["pass"]),
            str(summary["fail"]),
            str(summary["warn"]),
            str(summary["skip"]),
        )
    console.print(table)
    console.print()

    console.print(Rule(style="dim"))
    console.print(
        f"[bold]Totals:[/] "
        f"[green]{totals['pass']} passed[/], "
        f"[red]{totals['fail']} failed[/], "
        f"[yellow]{totals['warn']} warnings[/], "
        f"[dim]{totals['skip']} skipped[/]"
    )
    console.print()

    failed = len(run.packages) - passed
    noun = "packages" if any(p.kind == "helper" for p in run.packages) else "evaluations"
    if failed == 0:
        console.print(
            f"[bold green]{passed}/{len(run.packages)} {noun} passed all required checks[/]"
        )
    else:
        console.print(
            f"[bold]{passed}/{len(run.packages)} {noun} passed[/], [red]{failed} failed[/]"
        )


def _outcome_to_dict(outcome: Outcome) -> dict[str, Any]:
    return {
        "rule": outcome.rule.name if outcome.rule else None,
        "code": outcome.rule.code if outcome.rule else None,
        "category": outcome.rule.category if outcome.rule else None,
        "status": outcome.status,
        "message": outcome.message,
    }


def _diagnostic_to_dict(diagnostic: Diagnostic, root: Path | None) -> dict[str, Any]:
    return {
        "rule": diagnostic.rule.name if diagnostic.rule else None,
        "code": diagnostic.rule.code if diagnostic.rule else None,
        "category": diagnostic.rule.category if diagnostic.rule else None,
        "severity": diagnostic.severity,
        "status": diagnostic.status,
        "message": diagnostic.message,
        "file": _relative(diagnostic.file, root),
        "line": diagnostic.line,
        "column": diagnostic.column,
        "hint": diagnostic.hint,
    }


def package_to_dict(report: PackageReport, root: Path | None = None) -> dict[str, Any]:
    """One package's report as a JSON-serialisable mapping."""
    return {
        "name": report.name,
        "kind": report.kind,
        "passed": report.passed(),
        "skipped": report.skipped,
        "summary": report.summary(),
        "outcomes": [_outcome_to_dict(o) for o in report.outcomes],
        "diagnostics": [_diagnostic_to_dict(d, root) for d in report.diagnostics],
    }


def run_to_dict(run: RunReport) -> dict[str, Any]:
    """The whole run as a JSON-serialisable mapping.

    ``passed`` mirrors the CLI exit code: true when no rule failed in any package.
    File paths are relative to the root when they fall under it, so the document
    does not depend on the machine that produced it.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "version": __version__,
        "root": str(run.root),
        "passed": run.passed(),
        "summary": run.summary(),
        "packages": [package_to_dict(p, run.root) for p in run.packages],
    }


def render_json(run: RunReport) -> str:
    """The ``--output-format json`` document: :func:`run_to_dict` as indented JSON with a trailing newline."""
    return json.dumps(run_to_dict(run), indent=2) + "\n"
