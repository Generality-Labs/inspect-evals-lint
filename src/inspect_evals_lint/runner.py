"""Check registry and per-evaluation orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from inspect_evals_lint.checks import (
    check_custom_scorer_tests,
    check_custom_solver_tests,
    check_custom_tool_tests,
    check_e2e_test,
    check_eval_location,
    check_eval_yaml,
    check_external_dependencies,
    check_get_model_location,
    check_init_exports,
    check_main_file,
    check_private_api_imports,
    check_readme,
    check_record_to_sample_test,
    check_registry,
    check_sample_ids,
    check_sandbox_image_pinning,
    check_score_constants,
    check_task_overridable_defaults,
    check_tests_exist,
    check_tests_init,
    get_eval_path,
    get_test_path,
)
from inspect_evals_lint.config import LintConfig, load_config
from inspect_evals_lint.models import LintReport, LintResult
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions


@dataclass(frozen=True)
class LintContext:
    """Everything a check needs about the evaluation under review."""

    root: Path
    eval_name: str
    eval_path: Path
    test_path: Path | None
    config: LintConfig
    report: LintReport


CheckFn = Callable[[LintContext], object]

# Execution order matters: results are reported in this order, and later checks
# assume earlier ones (main_file before init_exports) have already reported.
# ``eval_location`` runs before all of these because it resolves ``eval_path``.
CHECKS: dict[str, CheckFn] = {
    "main_file": lambda c: check_main_file(c.eval_path, c.eval_name, c.report),
    "init_exports": lambda c: check_init_exports(c.eval_path, c.eval_name, c.report),
    "readme": lambda c: check_readme(
        c.eval_path,
        c.report,
        fallback=c.root / "README.md" if c.config.readme_location == "repo-root" else None,
    ),
    "private_api_imports": lambda c: check_private_api_imports(c.eval_path, c.report),
    "score_constants": lambda c: check_score_constants(c.eval_path, c.report),
    "get_model_location": lambda c: check_get_model_location(c.eval_path, c.report),
    "sample_ids": lambda c: check_sample_ids(c.eval_path, c.report),
    "task_overridable_defaults": lambda c: check_task_overridable_defaults(c.eval_path, c.report),
    "sandbox_image_pinning": lambda c: check_sandbox_image_pinning(
        c.eval_path, c.report, c.config.sandbox_image_allowlist
    ),
    "registry": lambda c: check_registry(c.root, c.eval_name, c.config, c.report),
    "eval_yaml": lambda c: check_eval_yaml(c.root, c.eval_name, c.config, c.report),
    "external_dependencies": lambda c: check_external_dependencies(
        c.root, c.eval_name, c.eval_path, c.config, c.report
    ),
    "tests_exist": lambda c: check_tests_exist(c.root, c.eval_name, c.config, c.report),
    "e2e_test": lambda c: check_e2e_test(c.test_path, c.report),
    "tests_init": lambda c: check_tests_init(
        c.test_path, c.report, tests_root=c.config.tests_dir(c.root)
    ),
    "record_to_sample_test": lambda c: check_record_to_sample_test(
        c.test_path, c.eval_path, c.report
    ),
    "custom_solver_tests": lambda c: check_custom_solver_tests(c.test_path, c.eval_path, c.report),
    "custom_scorer_tests": lambda c: check_custom_scorer_tests(c.test_path, c.eval_path, c.report),
    "custom_tool_tests": lambda c: check_custom_tool_tests(c.test_path, c.eval_path, c.report),
}

EVAL_LOCATION = "eval_location"

# The sections of docs/CHECKS.md. Reporting tools group results by these.
CHECK_CATEGORIES: dict[str, str] = {
    EVAL_LOCATION: "file_structure",
    "main_file": "file_structure",
    "init_exports": "file_structure",
    "registry": "file_structure",
    "eval_yaml": "file_structure",
    "readme": "file_structure",
    "private_api_imports": "code_quality",
    "score_constants": "code_quality",
    "external_dependencies": "code_quality",
    "tests_exist": "tests",
    "tests_init": "tests",
    "e2e_test": "tests",
    "record_to_sample_test": "tests",
    "custom_solver_tests": "tests",
    "custom_scorer_tests": "tests",
    "custom_tool_tests": "tests",
    "get_model_location": "best_practices",
    "sample_ids": "best_practices",
    "task_overridable_defaults": "best_practices",
    "sandbox_image_pinning": "best_practices",
}


def category_of(check_name: str) -> str | None:
    """The docs/CHECKS.md section a check belongs to; None for runner-level results such as ``invalid_check``."""
    return CHECK_CATEGORIES.get(check_name)


def get_all_check_names() -> list[str]:
    """Every check name, sorted."""
    return sorted([EVAL_LOCATION, *CHECKS])


def get_all_eval_names(repo_root: Path, config: LintConfig | None = None) -> list[str]:
    """Evaluation package names under ``config.source_root``.

    A directory counts when it has an ``__init__.py`` and is not hidden,
    underscore-prefixed, or listed in ``config.non_eval_dirs``.
    """
    config = config or load_config(repo_root)
    evals_dir = config.source_dir(repo_root)
    if not evals_dir.is_dir():
        return []
    return sorted(
        item.name
        for item in evals_dir.iterdir()
        if item.is_dir()
        and not item.name.startswith(("_", "."))
        and item.name not in config.non_eval_dirs
        and (item / "__init__.py").exists()
    )


def _should_run(name: str, only: str | None, config: LintConfig) -> bool:
    if only is not None and name != only:
        return False
    return name not in config.disabled_checks


def lint_evaluation(
    repo_root: Path,
    eval_name: str,
    config: LintConfig | None = None,
    check: str | None = None,
) -> LintReport:
    """Run every enabled check against one evaluation and return its report.

    Args:
        repo_root: Repository root.
        eval_name: Directory name of the evaluation under ``config.source_root``.
        config: Layout configuration; loaded from ``repo_root/pyproject.toml`` when omitted.
        check: Run only this check.
    """
    config = config or load_config(repo_root)
    report = LintReport(eval_name=eval_name)

    if eval_name in config.non_eval_dirs:
        report.add(
            LintResult(
                name="non_eval_directory",
                status="skip",
                message=f"'{eval_name}' is a utility module, not an evaluation",
            )
        )
        return report

    if check is not None and check not in get_all_check_names():
        report.add(
            LintResult(
                name="invalid_check",
                status="fail",
                message=f"Unknown check: '{check}'. Available checks: {get_all_check_names()}",
            )
        )
        return report

    if _should_run(EVAL_LOCATION, check, config):
        eval_path = check_eval_location(repo_root, eval_name, config, report)
    else:
        eval_path = get_eval_path(repo_root, eval_name, config)
    if eval_path is None:
        return report

    suppressions = load_suppressions(eval_path)
    context = LintContext(
        root=repo_root,
        eval_name=eval_name,
        eval_path=eval_path,
        test_path=get_test_path(repo_root, eval_name, config),
        config=config,
        report=report,
    )
    for name, run in CHECKS.items():
        if _should_run(name, check, config):
            run(context)

    apply_suppressions(report.results, suppressions)
    return report
