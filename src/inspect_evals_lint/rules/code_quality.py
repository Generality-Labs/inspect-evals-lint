"""Code-quality rules: private inspect_ai imports, literal score values, unscored reasons."""

from __future__ import annotations

import ast
from collections.abc import Iterable

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import (
    column_of,
    end_line_of,
    get_call_name,
    parse_failures,
    parse_python_files,
)

SCORE_LITERALS = ("C", "I", "CORRECT", "INCORRECT")


@rule(
    code="IECQ001",
    name="private_api_imports",
    category="code_quality",
    scopes=("eval", "helper"),
    summary="No imports from private inspect_ai modules",
)
def private_api_imports(ctx: LintContext) -> Iterable[Finding]:
    """No imports from private ``inspect_ai`` modules.

    ## What it does
    Flags ``from inspect_ai.<...>._<name> import ...`` wherever a dotted segment
    starts with an underscore. One diagnostic per import.

    ## Why is this bad?
    Private modules change without notice. An evaluation importing one breaks on
    the next ``inspect_ai`` release with no deprecation period.

    ## Example
    ```python
    from inspect_ai.scorer._metric import Score   # private
    ```
    Use instead:
    ```python
    from inspect_ai.scorer import Score
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    found = False
    for parsed in parsed_files.parsed:
        for node in ast.walk(parsed.tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("inspect_ai.")
                and "._" in node.module
            ):
                found = True
                yield Diagnostic(
                    f"Import from private inspect_ai module: {node.module}",
                    file=parsed.path,
                    line=node.lineno,
                    column=column_of(node),
                    end_line=end_line_of(node),
                )
    if not found:
        yield Outcome("pass", "No private API imports found")


@rule(
    code="IECQ002",
    name="score_constants",
    category="code_quality",
    scopes=("eval", "helper"),
    summary="Score() values use the CORRECT/INCORRECT constants, not string literals",
)
def score_constants(ctx: LintContext) -> Iterable[Finding]:
    """``Score()`` values use the ``CORRECT`` / ``INCORRECT`` constants, not string literals.

    ## What it does
    Flags ``Score(value="C")`` and the other literals ``"I"``, ``"CORRECT"`` and
    ``"INCORRECT"``. One diagnostic per call.

    ## Why is this bad?
    ``inspect_ai``'s metrics compare against the constants. A literal that drifts
    from them, or that ``inspect_ai`` later changes, scores silently wrong.

    ## Example
    ```python
    return Score(value="C")
    ```
    Use instead:
    ```python
    from inspect_ai.scorer import CORRECT

    return Score(value=CORRECT)
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    found = False
    for parsed in parsed_files.parsed:
        for node in ast.walk(parsed.tree):
            if not (isinstance(node, ast.Call) and get_call_name(node) == "Score"):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "value"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value in SCORE_LITERALS
                ):
                    found = True
                    yield Diagnostic(
                        f"Score(value={keyword.value.value!r}) uses a string literal",
                        file=parsed.path,
                        line=node.lineno,
                        column=column_of(node),
                        end_line=end_line_of(node),
                        hint="use the CORRECT / INCORRECT constants from inspect_ai.scorer",
                    )
    if not found:
        yield Outcome("pass", "Score() calls appear to use constants or computed values")


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


@rule(
    code="IECQ003",
    name="unscored_reason",
    category="code_quality",
    scopes=("eval", "helper"),
    summary="Score.unscored() passes a reason= and the legacy unscored_reason metadata key is gone",
)
def unscored_reason(ctx: LintContext) -> Iterable[Finding]:
    """``Score.unscored()`` passes a ``reason=`` and the legacy ``unscored_reason`` metadata key is gone.

    ## What it does
    Flags ``Score.unscored(...)`` calls whose ``reason=`` is missing, ``None`` or
    empty, and any occurrence of the string ``"unscored_reason"`` outside a
    docstring. Only attribute calls count, so a locally defined metric named
    ``unscored()`` is not mistaken for the constructor. One diagnostic per site.

    ## Why is this bad?
    ``Score.reason`` (inspect_ai 0.3.261) is the first-class record of why a sample
    was left unscored; metrics and log tooling read it there. The interim
    ``metadata["unscored_reason"]`` convention is superseded, and an unscored
    sample with no reason cannot be told apart from one the scorer forgot.

    ## Example
    ```python
    return Score.unscored(explanation="grader returned nothing")
    ```
    Use instead:
    ```python
    return Score.unscored(reason="grader_failed", explanation="grader returned nothing")
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    total_calls = 0
    sites: list[Diagnostic] = []
    for parsed in parsed_files.parsed:
        docstrings = _docstring_nodes(parsed.tree)
        for node in ast.walk(parsed.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "unscored"
            ):
                total_calls += 1
                if not _has_reason(node):
                    sites.append(
                        Diagnostic(
                            "Score.unscored() without reason=",
                            file=parsed.path,
                            line=node.lineno,
                            column=column_of(node),
                            end_line=end_line_of(node),
                            hint="pass reason= (e.g. 'grader_failed') so the sample records why it was left unscored",
                        )
                    )
            elif (
                isinstance(node, ast.Constant)
                and node.value == LEGACY_UNSCORED_KEY
                and id(node) not in docstrings
            ):
                sites.append(
                    Diagnostic(
                        f"'{LEGACY_UNSCORED_KEY}' metadata key is superseded by Score.reason (inspect_ai >= 0.3.261)",
                        file=parsed.path,
                        line=node.lineno,
                        column=column_of(node),
                        end_line=end_line_of(node),
                        hint="pass reason= to Score.unscored() and read score.reason",
                    )
                )

    # ast.walk is breadth-first, so sort to report sites in source order.
    yield from sorted(sites, key=lambda d: (str(d.file), d.line or 0, d.column or 0))
    if sites:
        return
    if total_calls == 0:
        yield Outcome("skip", "No Score.unscored() calls found")
    else:
        yield Outcome("pass", f"All {total_calls} Score.unscored() call(s) give a reason")
