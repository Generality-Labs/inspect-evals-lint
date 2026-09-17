"""Code-quality checks: private inspect_ai imports, literal score values, unscored reasons."""

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


UNSCORED_REASON_CHECK = "unscored_reason"
LEGACY_UNSCORED_KEY = "unscored_reason"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """``id()`` of every string constant that is a module, class or function docstring."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _has_reason(call: ast.Call) -> bool:
    """Whether ``reason=`` is passed with something other than a literal ``None`` or empty string."""
    for keyword in call.keywords:
        if keyword.arg is None:
            return True  # **kwargs: not knowable statically, taken at face value
        if keyword.arg == "reason":
            value = keyword.value
            return not (isinstance(value, ast.Constant) and value.value in (None, ""))
    return False


def check_unscored_reason(eval_path: Path, report: LintReport) -> None:
    """Fail on ``Score.unscored()`` without ``reason=`` and on the legacy ``"unscored_reason"`` metadata key.

    ``Score.reason`` (inspect_ai 0.3.261) is the first-class place to record why a
    sample was left unscored; metrics and log tooling read it there, and the
    interim ``metadata["unscored_reason"]`` convention is superseded. Only
    attribute calls (``Score.unscored(...)``) count, so a locally defined metric
    named ``unscored()`` is not mistaken for the constructor. One result per site.
    """
    parse_results = parse_python_files(eval_path)
    if add_parse_errors_to_report(UNSCORED_REASON_CHECK, parse_results.failed_paths, report):
        return

    total_calls = 0
    missing_reason: list[Issue] = []
    legacy_keys: list[Issue] = []
    for parsed in parse_results.parsed:
        docstrings = _docstring_nodes(parsed.tree)
        for node in ast.walk(parsed.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "unscored"
            ):
                total_calls += 1
                if not _has_reason(node):
                    missing_reason.append(Issue(str(parsed.path), node.lineno))
            elif (
                isinstance(node, ast.Constant)
                and node.value == LEGACY_UNSCORED_KEY
                and id(node) not in docstrings
            ):
                legacy_keys.append(Issue(str(parsed.path), node.lineno))

    # ast.walk is breadth-first, so sort to report sites in source order.
    for issue in sorted(missing_reason, key=lambda i: (i.file, i.line)):
        report.add(
            LintResult(
                name=UNSCORED_REASON_CHECK,
                status="fail",
                message=(
                    "Score.unscored() without reason=; pass reason= (e.g. 'grader_failed') "
                    "so the sample records why it was left unscored"
                ),
                file=issue.file,
                line=issue.line,
            )
        )
    for issue in sorted(legacy_keys, key=lambda i: (i.file, i.line)):
        report.add(
            LintResult(
                name=UNSCORED_REASON_CHECK,
                status="fail",
                message=(
                    f"'{LEGACY_UNSCORED_KEY}' metadata key is superseded by Score.reason "
                    "(inspect_ai >= 0.3.261); pass reason= to Score.unscored() and read score.reason"
                ),
                file=issue.file,
                line=issue.line,
            )
        )
    if missing_reason or legacy_keys:
        return
    if total_calls == 0:
        report.add(
            LintResult(
                name=UNSCORED_REASON_CHECK,
                status="skip",
                message="No Score.unscored() calls found",
            )
        )
    else:
        report.add(
            LintResult(
                name=UNSCORED_REASON_CHECK,
                status="pass",
                message=f"All {total_calls} Score.unscored() call(s) give a reason",
            )
        )
