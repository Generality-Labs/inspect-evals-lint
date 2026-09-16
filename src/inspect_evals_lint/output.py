"""Rich console rendering of lint reports."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from inspect_evals_lint import __version__
from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.models import LintReport, LintResult

console = Console()
stderr_console = Console(stderr=True)

CHECKS_DOC_URL = "https://github.com/Generality-Labs/inspect-evals-lint/blob/main/docs/CHECKS.md"

_STATUS_MARKUP = {
    "pass": "[bold green]PASS[/]",
    "fail": "[bold red]FAIL[/]",
    "warn": "[bold yellow]WARN[/]",
    "skip": "[dim]SKIP[/]",
    "suppressed": "[bold blue]SUPP[/]",
}
_MESSAGE_STYLE = {"pass": "", "fail": "red", "warn": "yellow", "skip": "dim", "suppressed": "blue"}


def get_status_text(status: str) -> Text:
    return Text.from_markup(_STATUS_MARKUP.get(status, status.upper()))


def _location(result: LintResult) -> str:
    location = result.file or ""
    if location and result.line:
        location += f":{result.line}"
    return location


def print_report(report: LintReport, config: LintConfig | None = None) -> None:
    """Print one evaluation's results with suppression hints for any failures."""
    source_root = config.source_root if config else "src"

    console.print()
    console.print(Rule(f"[bold]Lint Report: {report.eval_name}[/]", style="blue"))
    console.print()

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Status", width=6)
    table.add_column("Check", style="cyan")
    table.add_column("Message", overflow="fold")
    table.add_column("Location", style="dim", overflow="fold")

    failed_checks: list[str] = []
    for result in report.results:
        table.add_row(
            get_status_text(result.status),
            result.name,
            Text(result.message, style=_MESSAGE_STYLE.get(result.status, "")),
            _location(result),
        )
        if result.status == "fail":
            failed_checks.append(result.name)
    console.print(table)

    summary = report.summary()
    console.print()
    console.print(Rule(style="dim"))
    summary_parts: list[str] = []
    for status, label, style in (
        ("pass", "passed", "green"),
        ("fail", "failed", "red"),
        ("warn", "warnings", "yellow"),
        ("skip", "skipped", "dim"),
        ("suppressed", "suppressed", "blue"),
    ):
        if summary[status] > 0:
            summary_parts.append(f"[{style}]{summary[status]} {label}[/]")
    console.print(f"Summary: {', '.join(summary_parts)}")

    if report.passed():
        console.print("[bold green]All required checks passed![/]")
        return

    console.print("[bold red]Some checks failed.[/]")
    console.print()
    console.print("[dim]To suppress a check, add one of:[/]")
    for check in dict.fromkeys(failed_checks):
        console.print(f"  [cyan]# noautolint: {check}[/]  [dim](on the line)[/]")
    console.print(
        f"  [dim]Or add check name to[/] [cyan]{source_root}/{report.eval_name}/.noautolint[/]  [dim](eval-level)[/]"
    )
    console.print(
        f"  [dim]Or add check name to[/] [cyan]{source_root}/{report.eval_name}/<subdir>/.noautolint[/]  [dim](dir-level)[/]"
    )
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


def print_check_summary(reports: list[LintReport]) -> None:
    """Print per-check compliance across all evaluations, worst first."""
    check_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "warn": 0, "skip": 0, "suppressed": 0}
    )
    for report in reports:
        for result in report.results:
            check_counts[result.name][result.status] += 1

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
        check_counts.items(),
        key=lambda item: item[1]["pass"] / max(item[1]["pass"] + item[1]["fail"], 1),
    )
    for check_name, counts in sorted_checks:
        applicable = counts["pass"] + counts["fail"] + counts["warn"]
        rate_text = (
            _pass_rate_text(counts["pass"] / applicable) if applicable else Text("-", style="dim")
        )
        table.add_row(
            check_name,
            str(counts["pass"]),
            str(counts["fail"]),
            str(counts["warn"]),
            str(counts["skip"]),
            str(counts["suppressed"]),
            rate_text,
        )
    console.print(table)
    console.print()


def print_final_summary(reports: list[LintReport]) -> None:
    """Print the checks that ran, then failures and warnings grouped by check.

    When many evaluations fail the same check the grouping shows the pattern
    and every affected evaluation in one place.
    """
    check_names = sorted({r.name for report in reports for r in report.results})

    console.print()
    console.print(Rule("[bold]FINAL SUMMARY[/]", style="blue"))
    console.print()
    console.print(
        f"[bold]Checks run ({len(check_names)}):[/] "
        + ", ".join(f"[cyan]{name}[/]" for name in check_names)
    )
    console.print()

    if not _print_grouped_results(reports, status="fail", noun="failure", style="red"):
        console.print("[bold green]No failures.[/]")
    _print_grouped_results(reports, status="warn", noun="warning", style="yellow")


def _print_grouped_results(reports: list[LintReport], status: str, noun: str, style: str) -> bool:
    by_check: dict[str, list[tuple[str, LintResult]]] = {}
    for report in reports:
        for result in report.results:
            if result.status == status:
                by_check.setdefault(result.name, []).append((report.eval_name, result))
    if not by_check:
        return False

    console.print(f"[bold]{noun.capitalize()}s by check:[/]")
    for check_name, results in sorted(by_check.items(), key=lambda item: (-len(item[1]), item[0])):
        plural = noun if len(results) == 1 else f"{noun}s"
        console.print()
        console.print(f"[cyan]{check_name}[/] [{style}]({len(results)} {plural})[/]")

        table = Table(show_header=False, box=None, padding=(0, 1), pad_edge=False)
        table.add_column("Evaluation", style="cyan", no_wrap=True)
        table.add_column("Message", style=style, overflow="fold")
        table.add_column("Location", style="dim", overflow="fold")
        for eval_name, result in sorted(results, key=lambda item: item[0]):
            table.add_row(f"  {eval_name}", result.message, _location(result))
        console.print(table)
    console.print()
    return True


def print_overall_summary(reports: list[LintReport]) -> None:
    """Print one row per evaluation with pass/fail/warn/skip counts and totals."""
    console.print()
    console.print(Rule("[bold]OVERALL SUMMARY[/]", style="blue"))
    console.print()

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Status", width=6)
    table.add_column("Evaluation", style="cyan")
    table.add_column("Pass", justify="right", style="green")
    table.add_column("Fail", justify="right", style="red")
    table.add_column("Warn", justify="right", style="yellow")
    table.add_column("Skip", justify="right", style="dim")

    passed = 0
    totals = {"pass": 0, "fail": 0, "warn": 0, "skip": 0}
    for report in reports:
        summary = report.summary()
        for key in totals:
            totals[key] += summary[key]
        if report.passed():
            status = Text.from_markup("[bold green]PASS[/]")
            passed += 1
        else:
            status = Text.from_markup("[bold red]FAIL[/]")
        table.add_row(
            status,
            report.eval_name,
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

    failed = len(reports) - passed
    if failed == 0:
        console.print(
            f"[bold green]{passed}/{len(reports)} evaluations passed all required checks[/]"
        )
    else:
        console.print(
            f"[bold]{passed}/{len(reports)} evaluations passed[/], [red]{failed} failed[/]"
        )


def _relative_file(file: str | None, root: Path | None) -> str | None:
    """``file`` relative to ``root`` with forward slashes, when it is an absolute path under ``root``.

    Checks record absolute paths, which would tie the JSON to the machine that produced it.
    """
    if file is None or root is None:
        return file
    path = Path(file)
    if path.is_absolute() and path.is_relative_to(root):
        return path.relative_to(root).as_posix()
    return file


def report_to_dict(report: LintReport, root: Path | None = None) -> dict[str, Any]:
    """One evaluation's report as a JSON-serialisable mapping."""
    return {
        "name": report.eval_name,
        "passed": report.passed(),
        "summary": report.summary(),
        "results": [
            {
                "check": result.name,
                "status": result.status,
                "message": result.message,
                "file": _relative_file(result.file, root),
                "line": result.line,
            }
            for result in report.results
        ],
    }


def reports_to_dict(reports: list[LintReport], root: Path | None = None) -> dict[str, Any]:
    """Every report plus run-wide totals as a JSON-serialisable mapping.

    ``passed`` mirrors the CLI exit code: true when no check failed in any evaluation.
    """
    totals: dict[str, int] = dict.fromkeys(("pass", "fail", "warn", "skip", "suppressed"), 0)
    for report in reports:
        for status, count in report.summary().items():
            totals[status] += count
    return {
        "version": __version__,
        "root": str(root) if root is not None else None,
        "passed": all(report.passed() for report in reports),
        "evaluations_passed": sum(1 for report in reports if report.passed()),
        "evaluations_total": len(reports),
        "summary": totals,
        "evaluations": [report_to_dict(report, root) for report in reports],
    }


def render_json(reports: list[LintReport], root: Path | None = None) -> str:
    """The ``--json`` document: :func:`reports_to_dict` as indented JSON with a trailing newline."""
    return json.dumps(reports_to_dict(reports, root), indent=2) + "\n"
