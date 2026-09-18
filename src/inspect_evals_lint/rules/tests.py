"""Test-structure rules: test package exists, E2E test present, custom components tested."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import (
    ParsedFile,
    column_of,
    get_call_name,
    get_decorator_name,
    iter_python_files,
    safe_parse_file,
)


def _expected_test_dir(ctx: LintContext) -> Path:
    return ctx.tests_root / ctx.name


@rule(
    code="IETS001",
    name="tests_exist",
    category="tests",
    summary="A test directory exists for the evaluation",
)
def tests_exist(ctx: LintContext) -> Iterable[Finding]:
    """Check the evaluation has a test directory at ``<tests-root>/<name>/``.

    With ``tests-layout = "flat"``, test files directly under ``<tests-root>/``
    are accepted when that directory is absent, as single-evaluation
    repositories usually have.
    """
    if ctx.test_path:
        yield Outcome(
            "pass", f"Test directory exists at {ctx.test_path.relative_to(ctx.root).as_posix()}"
        )
        return
    expected = f"{ctx.config.tests_root}/{ctx.name}"
    if ctx.config.tests_layout == "flat":
        expected += f" (or test files directly under {ctx.config.tests_root}/)"
    yield Diagnostic(f"Missing test directory: {expected}", file=_expected_test_dir(ctx))


def _get_eval_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to ``inspect_ai.eval`` / ``eval_async`` (including ``as`` aliases)."""
    aliases: set[str] = {"eval", "eval_async"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "inspect_ai" in node.module:
            aliases.update(
                alias.asname
                for alias in node.names
                if alias.name in ("eval", "eval_async") and alias.asname
            )
    return aliases


def _has_eval_call(tree: ast.AST) -> bool:
    eval_names = _get_eval_aliases(tree)
    return any(
        isinstance(node, ast.Call) and get_call_name(node) in eval_names for node in ast.walk(tree)
    )


def _no_test_dir(ctx: LintContext) -> Diagnostic:
    return Diagnostic("No test directory exists", file=_expected_test_dir(ctx))


@rule(
    code="IETS003",
    name="e2e_test",
    category="tests",
    summary="Some test runs eval() against mockllm/model",
)
def e2e_test(ctx: LintContext) -> Iterable[Finding]:
    """Check some test calls ``eval()`` or ``eval_async()`` (or an alias imported from ``inspect_ai``) and mentions ``mockllm/model``.

    An end-to-end run against the mock model catches wiring mistakes without
    spending tokens.
    """
    if ctx.test_path is None:
        yield _no_test_dir(ctx)
        return

    unparsable: list[Diagnostic] = []
    for py_file in ctx.test_path.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except (SyntaxError, UnicodeDecodeError, OSError) as e:
            unparsable.append(Diagnostic(f"Could not parse file: {e}", file=py_file, line=1))
            continue
        if _has_eval_call(tree) and "mockllm/model" in content:
            yield Outcome("pass", "E2E test with eval() and mockllm/model found")
            return

    if unparsable:
        yield from unparsable
        return
    yield Diagnostic(
        "No E2E test found",
        file=ctx.test_path,
        hint="add a test that calls eval() on the task with model='mockllm/model'",
    )


def _first_mention(files: Iterable[Path], needle: str) -> tuple[Path, int] | None:
    for py_file in files:
        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines, start=1):
            if needle in line:
                return py_file, i
    return None


@rule(
    code="IETS004",
    name="record_to_sample_test",
    category="tests",
    summary="record_to_sample is exercised by a test when the evaluation uses it",
)
def record_to_sample_test(ctx: LintContext) -> Iterable[Finding]:
    """Check ``record_to_sample`` is referenced by a test when the evaluation defines or uses one."""
    if ctx.test_path is None:
        yield _no_test_dir(ctx)
        return

    mention = _first_mention(iter_python_files(ctx), "record_to_sample")
    if mention is None:
        yield Outcome("skip", "Evaluation does not use record_to_sample")
        return

    if _first_mention(sorted(ctx.test_path.rglob("*.py")), "record_to_sample") is not None:
        yield Outcome("pass", "record_to_sample is tested")
    else:
        file, line = mention
        yield Diagnostic(
            "record_to_sample is used but no test mentions it",
            file=file,
            line=line,
            hint="add a test that calls record_to_sample on a real record",
        )


def _find_decorated_functions(
    ctx: LintContext, decorator_name: str
) -> tuple[list[tuple[Path, str, int, int | None]], list[Diagnostic]]:
    """``(file, name, line, column)`` of functions decorated with ``decorator_name``, plus parse diagnostics."""
    functions: list[tuple[Path, str, int, int | None]] = []
    failed: list[Diagnostic] = []
    for py_file in iter_python_files(ctx):
        outcome = safe_parse_file(py_file)
        if not isinstance(outcome, ParsedFile):
            failed.append(
                Diagnostic(f"Could not parse file: {outcome.error}", file=py_file, line=1)
            )
            continue
        functions.extend(
            (py_file, node.name, node.lineno, column_of(node))
            for node in ast.walk(outcome.tree)
            if isinstance(node, ast.FunctionDef)
            and any(get_decorator_name(d) == decorator_name for d in node.decorator_list)
        )
    return functions, failed


def _custom_component_tests(ctx: LintContext, decorator_type: str) -> Iterable[Finding]:
    plural = f"{decorator_type}s"
    if ctx.test_search_path is None:
        yield _no_test_dir(ctx)
        return

    functions, failed = _find_decorated_functions(ctx, decorator_type)
    yield from failed

    if not functions:
        yield Outcome("skip", f"No custom {plural} found")
        return

    untested = 0
    for file, name, line, column in functions:
        if _first_mention(sorted(ctx.test_search_path.rglob("*.py")), name) is not None:
            continue
        untested += 1
        yield Diagnostic(
            f"@{decorator_type} {name}() is not mentioned by any test",
            file=file,
            line=line,
            column=column,
            hint=f"add a test that exercises {name}()",
        )
    if not untested:
        yield Outcome("pass", f"All {len(functions)} custom {plural} appear tested")


@rule(
    code="IETS005",
    name="custom_solver_tests",
    category="tests",
    scopes=("eval", "helper"),
    summary="Every @solver function name appears somewhere in the tests",
)
def custom_solver_tests(ctx: LintContext) -> Iterable[Finding]:
    """Check every ``@solver`` function name appears somewhere in the tests. A presence check, not a quality check."""
    yield from _custom_component_tests(ctx, "solver")


@rule(
    code="IETS006",
    name="custom_scorer_tests",
    category="tests",
    scopes=("eval", "helper"),
    summary="Every @scorer function name appears somewhere in the tests",
)
def custom_scorer_tests(ctx: LintContext) -> Iterable[Finding]:
    """Check every ``@scorer`` function name appears somewhere in the tests. A presence check, not a quality check."""
    yield from _custom_component_tests(ctx, "scorer")


@rule(
    code="IETS007",
    name="custom_tool_tests",
    category="tests",
    scopes=("eval", "helper"),
    summary="Every @tool function name appears somewhere in the tests",
)
def custom_tool_tests(ctx: LintContext) -> Iterable[Finding]:
    """Check every ``@tool`` function name appears somewhere in the tests. A presence check, not a quality check."""
    yield from _custom_component_tests(ctx, "tool")


EXCLUDED_TEST_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache"}


@rule(
    code="IETS002",
    name="tests_init",
    category="tests",
    scopes=("eval", "helper"),
    summary="The test directory and its sub-directories contain __init__.py",
)
def tests_init(ctx: LintContext) -> Iterable[Finding]:
    """Check the test directory and every sub-directory has an ``__init__.py``.

    Per-evaluation test trees with duplicate module basenames collide during
    pytest collection without them. Skipped when the tests live directly under
    the tests root (flat layout), where there is nothing to collide with, and
    for a helper package with no ``tests/<name>/`` directory, since its tests
    may live anywhere. One diagnostic per directory.
    """
    if ctx.test_path is None:
        if ctx.kind == "eval":
            yield _no_test_dir(ctx)
        else:
            yield Outcome("skip", "No test directory named after this package")
        return
    if ctx.test_path == ctx.tests_root:
        yield Outcome(
            "skip", "Tests live directly under the tests root; __init__.py files not required"
        )
        return

    missing = [
        d
        for d in (ctx.test_path, *sorted(ctx.test_path.rglob("*")))
        if d.is_dir() and d.name not in EXCLUDED_TEST_DIRS and not (d / "__init__.py").exists()
    ]
    for directory in missing:
        yield Diagnostic(
            "Test directory is missing __init__.py",
            file=directory,
            hint="add an empty __init__.py so pytest collects it as a package",
        )
    if not missing:
        yield Outcome("pass", "Test directory has __init__.py")
