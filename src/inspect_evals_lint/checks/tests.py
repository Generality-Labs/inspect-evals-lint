"""Test-structure checks: test package exists, E2E test present, custom components tested."""

from __future__ import annotations

import ast
from pathlib import Path

from inspect_evals_lint.checks.utils import (
    ParsedFile,
    add_parse_errors_to_report,
    get_call_name,
    get_decorator_name,
    iter_python_files,
    safe_parse_file,
)
from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.models import LintReport, LintResult


def get_test_path(repo_root: Path, eval_name: str, config: LintConfig) -> Path | None:
    """The evaluation's test directory, or None if it does not exist.

    ``<tests_root>/<eval_name>/`` when present; with the ``flat`` layout, ``tests_root``
    itself when it holds test files directly.
    """
    per_eval = config.tests_dir(repo_root) / eval_name
    if per_eval.is_dir():
        return per_eval
    tests_root = config.tests_dir(repo_root)
    if config.tests_layout == "flat" and _has_test_files(tests_root):
        return tests_root
    return None


def _has_test_files(directory: Path) -> bool:
    return directory.is_dir() and any(
        path.is_file() for path in (*directory.glob("test_*.py"), *directory.glob("*_test.py"))
    )


def check_tests_exist(
    repo_root: Path, eval_name: str, config: LintConfig, report: LintReport
) -> Path | None:
    """Check the evaluation has a test directory; returns its path."""
    test_path = get_test_path(repo_root, eval_name, config)
    if test_path:
        report.add(
            LintResult(
                name="tests_exist",
                status="pass",
                message=f"Test directory exists at {test_path.relative_to(repo_root).as_posix()}",
            )
        )
        return test_path
    expected = f"{config.tests_root}/{eval_name}"
    if config.tests_layout == "flat":
        expected += f" (or test files directly under {config.tests_root}/)"
    report.add(
        LintResult(
            name="tests_exist",
            status="fail",
            message=f"Missing test directory: {expected}",
        )
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


def _no_test_dir(check_name: str, report: LintReport) -> None:
    report.add(LintResult(name=check_name, status="fail", message="No test directory exists"))


def check_e2e_test(test_path: Path | None, report: LintReport) -> None:
    """Check some test calls ``eval()``/``eval_async()`` against ``mockllm/model``."""
    if test_path is None:
        _no_test_dir("e2e_test", report)
        return

    unparsable: list[str] = []
    found_e2e_test = False
    for py_file in test_path.rglob("*.py"):
        try:
            content = py_file.read_text()
            tree = ast.parse(content)
        except (SyntaxError, UnicodeDecodeError, OSError):
            unparsable.append(str(py_file))
            continue
        if _has_eval_call(tree) and "mockllm/model" in content:
            found_e2e_test = True
            break

    if found_e2e_test:
        report.add(
            LintResult(
                name="e2e_test",
                status="pass",
                message="E2E test with eval() and mockllm/model found",
            )
        )
    elif add_parse_errors_to_report("e2e_test", unparsable, report):
        return
    else:
        report.add(
            LintResult(
                name="e2e_test",
                status="fail",
                message="No E2E test found (need test file with eval() call and mockllm/model)",
            )
        )


def _any_file_mentions(directory: Path, needle: str) -> bool:
    for py_file in directory.rglob("*.py"):
        try:
            if needle in py_file.read_text():
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def check_record_to_sample_test(
    test_path: Path | None, eval_path: Path, report: LintReport
) -> None:
    """Check ``record_to_sample`` is referenced by a test when the eval defines or uses one."""
    if test_path is None:
        _no_test_dir("record_to_sample_test", report)
        return

    if not _any_file_mentions(eval_path, "record_to_sample"):
        report.add(
            LintResult(
                name="record_to_sample_test",
                status="skip",
                message="Evaluation does not use record_to_sample",
            )
        )
        return

    if _any_file_mentions(test_path, "record_to_sample"):
        report.add(
            LintResult(
                name="record_to_sample_test",
                status="pass",
                message="record_to_sample is tested",
            )
        )
    else:
        report.add(
            LintResult(
                name="record_to_sample_test",
                status="fail",
                message="record_to_sample function exists but is not tested",
            )
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
    report: LintReport,
    decorator_type: str,
) -> None:
    check_name = f"custom_{decorator_type}_tests"
    plural = f"{decorator_type}s"

    if test_path is None:
        _no_test_dir(check_name, report)
        return

    functions, failed = _find_decorated_functions(eval_path, decorator_type)
    if add_parse_errors_to_report(check_name, failed, report):
        return

    if not functions:
        report.add(LintResult(name=check_name, status="skip", message=f"No custom {plural} found"))
        return

    untested = [name for _, name, _ in functions if not _any_file_mentions(test_path, name)]
    if untested:
        report.add(
            LintResult(
                name=check_name,
                status="fail",
                message=f"Custom {plural} without apparent tests: {untested[:5]}",
            )
        )
    else:
        report.add(
            LintResult(
                name=check_name,
                status="pass",
                message=f"All {len(functions)} custom {plural} appear tested",
            )
        )


def check_custom_solver_tests(test_path: Path | None, eval_path: Path, report: LintReport) -> None:
    """Check every ``@solver`` function name appears somewhere in the tests."""
    _check_custom_decorated_tests(test_path, eval_path, report, "solver")


def check_custom_scorer_tests(test_path: Path | None, eval_path: Path, report: LintReport) -> None:
    """Check every ``@scorer`` function name appears somewhere in the tests."""
    _check_custom_decorated_tests(test_path, eval_path, report, "scorer")


def check_custom_tool_tests(test_path: Path | None, eval_path: Path, report: LintReport) -> None:
    """Check every ``@tool`` function name appears somewhere in the tests."""
    _check_custom_decorated_tests(test_path, eval_path, report, "tool")


EXCLUDED_TEST_DIRS = {"__pycache__", ".mypy_cache", "utils"}


def check_tests_init(
    test_path: Path | None, report: LintReport, tests_root: Path | None = None
) -> None:
    """Check the test directory and every sub-directory has an ``__init__.py``.

    Skipped when ``test_path`` is ``tests_root`` itself (flat layout): the packages
    guard against basename collisions between per-evaluation test directories,
    and a flat tree has none.
    """
    if test_path is None:
        _no_test_dir("tests_init", report)
        return
    if tests_root is not None and test_path == tests_root:
        report.add(
            LintResult(
                name="tests_init",
                status="skip",
                message="Tests live directly under the tests root; __init__.py files not required",
            )
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
        report.add(
            LintResult(
                name="tests_init",
                status="fail",
                message=f"Test directories missing __init__.py: {missing_init}",
            )
        )
    else:
        report.add(
            LintResult(
                name="tests_init",
                status="pass",
                message="Test directory has __init__.py",
            )
        )
