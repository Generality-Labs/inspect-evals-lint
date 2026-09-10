"""Code-quality checks: private inspect_ai imports and literal score values."""

from __future__ import annotations

import ast
from pathlib import Path

from inspect_evals_lint.checks.utils import (
    Issue,
    add_parse_errors_to_report,
    get_call_name,
    parse_python_files,
)
from inspect_evals_lint.models import LintReport, LintResult

SCORE_LITERALS = ("C", "I", "CORRECT", "INCORRECT")


def check_private_api_imports(eval_path: Path, report: LintReport) -> None:
    """Fail on ``from inspect_ai.<...>._<private> import ...``; one result per import site."""
    parse_results = parse_python_files(eval_path)
    if add_parse_errors_to_report("private_api_imports", parse_results.failed_paths, report):
        return

    issues: list[Issue] = []
    for parsed in parse_results.parsed:
        for node in ast.walk(parsed.tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("inspect_ai.")
                and "._" in node.module
            ):
                issues.append(Issue(str(parsed.path), node.lineno, node.module))

    if issues:
        for issue in issues:
            report.add(
                LintResult(
                    name="private_api_imports",
                    status="fail",
                    message=f"Import from private inspect_ai module: {issue.detail}",
                    file=issue.file,
                    line=issue.line,
                )
            )
    else:
        report.add(
            LintResult(
                name="private_api_imports",
                status="pass",
                message="No private API imports found",
            )
        )


def check_score_constants(eval_path: Path, report: LintReport) -> None:
    """Fail when ``Score(value="C")``-style literals are used instead of ``CORRECT``/``INCORRECT``."""
    parse_results = parse_python_files(eval_path)
    if add_parse_errors_to_report("score_constants", parse_results.failed_paths, report):
        return

    issues: list[Issue] = []
    for parsed in parse_results.parsed:
        for node in ast.walk(parsed.tree):
            if not (isinstance(node, ast.Call) and get_call_name(node) == "Score"):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "value"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value in SCORE_LITERALS
                ):
                    issues.append(Issue(str(parsed.path), node.lineno))

    if issues:
        report.add(
            LintResult(
                name="score_constants",
                status="fail",
                message=f"Found {len(issues)} Score() calls with literal strings - consider using CORRECT/INCORRECT constants",
            )
        )
    else:
        report.add(
            LintResult(
                name="score_constants",
                status="pass",
                message="Score() calls appear to use constants or computed values",
            )
        )
