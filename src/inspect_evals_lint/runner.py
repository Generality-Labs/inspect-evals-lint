"""Check registry and per-package orchestration."""

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
    check_model_role_resolution,
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
    check_unscored_reason,
    get_eval_path,
    get_test_path,
)
from inspect_evals_lint.checks.file_structure import is_package
from inspect_evals_lint.config import LintConfig, load_config
from inspect_evals_lint.models import LintReport, LintResult, PackageKind
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions


@dataclass(frozen=True)
class LintContext:
    """Everything a check needs about the package under review."""

    root: Path
    eval_name: str
    eval_path: Path
    test_path: Path | None
    """``<tests_root>/<eval_name>/`` (or the flat tests root), when it exists."""
    test_search_path: Path | None
    """Where tests for custom components are looked for: ``test_path`` for an evaluation, the whole tests root for a helper."""
    config: LintConfig
    report: LintReport
    kind: PackageKind = "eval"


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
    "unscored_reason": lambda c: check_unscored_reason(c.eval_path, c.report),
    "get_model_location": lambda c: check_get_model_location(c.eval_path, c.report),
    "model_role_resolution": lambda c: check_model_role_resolution(
        c.eval_path, c.report, c.config.model_role_allowlist
    ),
    "sample_ids": lambda c: check_sample_ids(c.eval_path, c.report),
    "task_overridable_defaults": lambda c: check_task_overridable_defaults(c.eval_path, c.report),
    "sandbox_image_pinning": lambda c: check_sandbox_image_pinning(
        c.eval_path, c.report, c.config.sandbox_image_allowlist
    ),
    "registry": lambda c: check_registry(c.root, c.eval_name, c.config, c.report),
    "eval_yaml": lambda c: check_eval_yaml(c.root, c.eval_name, c.config, c.report),
    "external_dependencies": lambda c: check_external_dependencies(
        c.root, c.eval_name, c.eval_path, c.config, c.report, kind=c.kind
    ),
    "tests_exist": lambda c: check_tests_exist(c.root, c.eval_name, c.config, c.report),
    "e2e_test": lambda c: check_e2e_test(c.test_path, c.report),
    "tests_init": lambda c: check_tests_init(
        c.test_path,
        c.report,
        tests_root=c.config.tests_dir(c.root),
        required=c.kind == "eval",
    ),
    "record_to_sample_test": lambda c: check_record_to_sample_test(
        c.test_path, c.eval_path, c.report
    ),
    "custom_solver_tests": lambda c: check_custom_solver_tests(
        c.test_search_path, c.eval_path, c.report
    ),
    "custom_scorer_tests": lambda c: check_custom_scorer_tests(
        c.test_search_path, c.eval_path, c.report
    ),
    "custom_tool_tests": lambda c: check_custom_tool_tests(
        c.test_search_path, c.eval_path, c.report
    ),
}

EVAL_LOCATION = "eval_location"

# The sections of docs/CHECKS.md. Reporting tools group results by these, and
# downstream consumers (badges, the register lint service) know exactly this
# set: adding a category is a breaking change for them, so a new check goes in
# one of these four.
CATEGORIES: tuple[str, ...] = ("file_structure", "code_quality", "tests", "best_practices")

CHECK_CATEGORIES: dict[str, str] = {
    EVAL_LOCATION: "file_structure",
    "main_file": "file_structure",
    "init_exports": "file_structure",
    "registry": "file_structure",
    "eval_yaml": "file_structure",
    "readme": "file_structure",
    "private_api_imports": "code_quality",
    "score_constants": "code_quality",
    "unscored_reason": "code_quality",
    "external_dependencies": "code_quality",
    "tests_exist": "tests",
    "tests_init": "tests",
    "e2e_test": "tests",
    "record_to_sample_test": "tests",
    "custom_solver_tests": "tests",
    "custom_scorer_tests": "tests",
    "custom_tool_tests": "tests",
    "get_model_location": "best_practices",
    "model_role_resolution": "best_practices",
    "sample_ids": "best_practices",
    "task_overridable_defaults": "best_practices",
    "sandbox_image_pinning": "best_practices",
}

# Checks that also apply to helper packages: shared code has no task, README,
# registration or eval.yaml of its own, but everything about how it scores,
# what it imports and whether its components are tested carries over.
HELPER_CHECKS: frozenset[str] = frozenset(
    {
        EVAL_LOCATION,
        "private_api_imports",
        "score_constants",
        "unscored_reason",
        "external_dependencies",
        "tests_init",
        "custom_solver_tests",
        "custom_scorer_tests",
        "custom_tool_tests",
        "get_model_location",
        "model_role_resolution",
        "sample_ids",
        "task_overridable_defaults",
        "sandbox_image_pinning",
    }
)

CHECK_SCOPES: dict[str, frozenset[PackageKind]] = {
    name: frozenset({"eval", "helper"}) if name in HELPER_CHECKS else frozenset({"eval"})
    for name in (EVAL_LOCATION, *CHECKS)
}
"""The package kinds each check applies to."""


def category_of(check_name: str) -> str | None:
    """The docs/CHECKS.md section a check belongs to; None for runner-level results such as ``invalid_check``."""
    return CHECK_CATEGORIES.get(check_name)


def get_all_check_names() -> list[str]:
    """Every check name, sorted."""
    return sorted([EVAL_LOCATION, *CHECKS])


def _candidate_dirs(repo_root: Path, config: LintConfig) -> list[Path]:
    evals_dir = config.source_dir(repo_root)
    if not evals_dir.is_dir():
        return []
    return sorted(
        item
        for item in evals_dir.iterdir()
        if is_package(item) and not item.name.startswith(("_", "."))
    )


def get_all_eval_names(repo_root: Path, config: LintConfig | None = None) -> list[str]:
    """Evaluation package names under ``config.source_root``.

    A directory counts when it has an ``__init__.py`` and is not hidden,
    underscore-prefixed, or listed in ``config.helper_dirs`` or ``config.ignore_dirs``.
    """
    config = config or load_config(repo_root)
    excluded = config.helper_dirs | config.ignore_dirs
    return [item.name for item in _candidate_dirs(repo_root, config) if item.name not in excluded]


def get_all_helper_names(repo_root: Path, config: LintConfig | None = None) -> list[str]:
    """Helper package names under ``config.source_root``: entries of ``helper_dirs`` that are packages."""
    config = config or load_config(repo_root)
    return [
        item.name for item in _candidate_dirs(repo_root, config) if item.name in config.helper_dirs
    ]


def package_kind(eval_name: str, config: LintConfig) -> PackageKind:
    """Whether ``eval_name`` is linted as an evaluation or as a helper package."""
    return "helper" if eval_name in config.helper_dirs else "eval"


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
    """Run every enabled check against one package and return its report.

    Args:
        repo_root: Repository root.
        eval_name: Directory name of the evaluation or helper package under ``config.source_root``.
        config: Layout configuration; loaded from ``repo_root/pyproject.toml`` when omitted.
        check: Run only this check.
    """
    config = config or load_config(repo_root)
    kind = package_kind(eval_name, config)
    report = LintReport(eval_name=eval_name, kind=kind)

    if eval_name in config.ignore_dirs:
        report.add(
            LintResult(
                name="ignored_directory",
                status="skip",
                message=f"'{eval_name}' is listed in ignore-dirs",
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

    if check is not None and kind not in CHECK_SCOPES[check]:
        report.add(
            LintResult(
                name=check,
                status="skip",
                message=f"'{check}' does not apply to a {kind} package",
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
    test_path = get_test_path(repo_root, eval_name, config)
    tests_root = config.tests_dir(repo_root)
    context = LintContext(
        root=repo_root,
        eval_name=eval_name,
        eval_path=eval_path,
        test_path=test_path,
        test_search_path=test_path
        if kind == "eval"
        else (tests_root if tests_root.is_dir() else None),
        config=config,
        report=report,
        kind=kind,
    )
    for name, run in CHECKS.items():
        if kind in CHECK_SCOPES[name] and _should_run(name, check, config):
            run(context)

    apply_suppressions(report.results, suppressions)
    return report
