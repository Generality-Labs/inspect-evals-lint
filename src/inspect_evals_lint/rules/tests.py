"""Test-structure checks: test package exists, E2E test present, custom components tested."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from inspect_evals_lint.context import LintContext, get_test_path
from inspect_evals_lint.models import LintResult
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import (
    ParsedFile,
    get_call_name,
    get_decorator_name,
    iter_python_files,
    parse_error_result,
    safe_parse_file,
)


@rule(
    code="IETS001",
    name="tests_exist",
    category="tests",
    summary="A test directory exists for the evaluation",
)
def tests_exist(ctx: LintContext) -> Iterable[LintResult]:
    """Check the evaluation has a test directory; returns its path."""
    repo_root, eval_name, config = ctx.root, ctx.name, ctx.config
    test_path = get_test_path(repo_root, eval_name, config)
    if test_path:
        yield LintResult(
            name="tests_exist",
            status="pass",
            message=f"Test directory exists at {test_path.relative_to(repo_root).as_posix()}",
        )

        return
    expected = f"{config.tests_root}/{eval_name}"
    if config.tests_layout == "flat":
        expected += f" (or test files directly under {config.tests_root}/)"
    yield LintResult(
        name="tests_exist",
        status="fail",
        message=f"Missing test directory: {expected}",
    )

    return None


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


def _no_test_dir(check_name: str) -> LintResult:
    return LintResult(name=check_name, status="fail", message="No test directory exists")


@rule(
    code="IETS003",
    name="e2e_test",
    category="tests",
    summary="Some test runs eval() against mockllm/model",
)
def e2e_test(ctx: LintContext) -> Iterable[LintResult]:
    """Check some test calls ``eval()``/``eval_async()`` against ``mockllm/model``."""
    test_path = ctx.test_path
    if test_path is None:
        yield _no_test_dir("e2e_test")
        return

    unparsable: list[str] = []
    found_e2e_test = False
    for py_file in test_path.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except (SyntaxError, UnicodeDecodeError, OSError):
            unparsable.append(str(py_file))
            continue
        if _has_eval_call(tree) and "mockllm/model" in content:
            found_e2e_test = True
            break

    if found_e2e_test:
        yield LintResult(
            name="e2e_test",
            status="pass",
            message="E2E test with eval() and mockllm/model found",
        )

    elif failed := parse_error_result("e2e_test", unparsable):
        yield failed
        return
    else:
        yield LintResult(
            name="e2e_test",
            status="fail",
            message="No E2E test found (need test file with eval() call and mockllm/model)",
        )


def _any_file_mentions(directory: Path, needle: str) -> bool:
    for py_file in directory.rglob("*.py"):
        try:
            if needle in py_file.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


@rule(
    code="IETS004",
    name="record_to_sample_test",
    category="tests",
    summary="record_to_sample is exercised by a test when the evaluation uses it",
)
def record_to_sample_test(ctx: LintContext) -> Iterable[LintResult]:
    """Check ``record_to_sample`` is referenced by a test when the eval defines or uses one."""
    test_path, eval_path = ctx.test_path, ctx.path
    if test_path is None:
        yield _no_test_dir("record_to_sample_test")
        return

    if not _any_file_mentions(eval_path, "record_to_sample"):
        yield LintResult(
            name="record_to_sample_test",
            status="skip",
            message="Evaluation does not use record_to_sample",
        )

        return

    if _any_file_mentions(test_path, "record_to_sample"):
        yield LintResult(
            name="record_to_sample_test",
            status="pass",
            message="record_to_sample is tested",
        )

    else:
        yield LintResult(
            name="record_to_sample_test",
            status="fail",
            message="record_to_sample function exists but is not tested",
        )


def _find_decorated_functions(
    eval_path: Path, decorator_name: str
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """``(file, name, line)`` of functions decorated with ``decorator_name``, plus unparsable files."""
    functions: list[tuple[str, str, int]] = []
    failed: list[str] = []
    for py_file in iter_python_files(eval_path):
        outcome = safe_parse_file(py_file)
        if not isinstance(outcome, ParsedFile):
            failed.append(str(py_file))
            continue
        functions.extend(
            (str(py_file), node.name, node.lineno)
            for node in ast.walk(outcome.tree)
            if isinstance(node, ast.FunctionDef)
            and any(get_decorator_name(d) == decorator_name for d in node.decorator_list)
        )
    return functions, failed


def _check_custom_decorated_tests(
    test_path: Path | None,
    eval_path: Path,
    decorator_type: str,
) -> Iterable[LintResult]:
    check_name = f"custom_{decorator_type}_tests"
    plural = f"{decorator_type}s"

    if test_path is None:
        yield _no_test_dir(check_name)
        return

    functions, failed = _find_decorated_functions(eval_path, decorator_type)
    if failed := parse_error_result(check_name, failed):
        yield failed
        return

    if not functions:
        yield LintResult(name=check_name, status="skip", message=f"No custom {plural} found")
        return

    untested = [name for _, name, _ in functions if not _any_file_mentions(test_path, name)]
    if untested:
        yield LintResult(
            name=check_name,
            status="fail",
            message=f"Custom {plural} without apparent tests: {untested[:5]}",
        )

    else:
        yield LintResult(
            name=check_name,
            status="pass",
            message=f"All {len(functions)} custom {plural} appear tested",
        )


@rule(
    code="IETS005",
    name="custom_solver_tests",
    category="tests",
    scopes=("eval", "helper"),
    summary="Every @solver function name appears somewhere in the tests",
)
def custom_solver_tests(ctx: LintContext) -> Iterable[LintResult]:
    """Check every ``@solver`` function name appears somewhere in the tests."""
    yield from _check_custom_decorated_tests(ctx.test_search_path, ctx.path, "solver")


@rule(
    code="IETS006",
    name="custom_scorer_tests",
    category="tests",
    scopes=("eval", "helper"),
    summary="Every @scorer function name appears somewhere in the tests",
)
def custom_scorer_tests(ctx: LintContext) -> Iterable[LintResult]:
    """Check every ``@scorer`` function name appears somewhere in the tests."""
    yield from _check_custom_decorated_tests(ctx.test_search_path, ctx.path, "scorer")


@rule(
    code="IETS007",
    name="custom_tool_tests",
    category="tests",
    scopes=("eval", "helper"),
    summary="Every @tool function name appears somewhere in the tests",
)
def custom_tool_tests(ctx: LintContext) -> Iterable[LintResult]:
    """Check every ``@tool`` function name appears somewhere in the tests."""
    yield from _check_custom_decorated_tests(ctx.test_search_path, ctx.path, "tool")


EXCLUDED_TEST_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache"}


@rule(
    code="IETS002",
    name="tests_init",
    category="tests",
    scopes=("eval", "helper"),
    summary="The test directory and its sub-directories contain __init__.py",
)
def tests_init(ctx: LintContext) -> Iterable[LintResult]:
    """Check the test directory and every sub-directory has an ``__init__.py``.

    Skipped when ``test_path`` is ``tests_root`` itself (flat layout): the packages
    guard against basename collisions between per-evaluation test directories,
    and a flat tree has none. With ``required=False`` a missing test directory is
    a skip rather than a failure, for helper packages whose tests may live anywhere.
    """
    test_path, tests_root = ctx.test_path, ctx.tests_root
    required = ctx.kind == "eval"
    if test_path is None:
        if required:
            yield _no_test_dir("tests_init")
        else:
            yield LintResult(
                name="tests_init",
                status="skip",
                message="No test directory named after this package",
            )

        return
    if test_path == tests_root:
        yield LintResult(
            name="tests_init",
            status="skip",
            message="Tests live directly under the tests root; __init__.py files not required",
        )

        return

    missing_init: list[str] = []
    if not (test_path / "__init__.py").exists():
        missing_init.append(test_path.name)
    for item in test_path.rglob("*"):
        if (
            item.is_dir()
            and item.name not in EXCLUDED_TEST_DIRS
            and not (item / "__init__.py").exists()
        ):
            missing_init.append(str(item.relative_to(test_path)))

    if missing_init:
        yield LintResult(
            name="tests_init",
            status="fail",
            message=f"Test directories missing __init__.py: {missing_init}",
        )

    else:
        yield LintResult(
            name="tests_init",
            status="pass",
            message="Test directory has __init__.py",
        )
