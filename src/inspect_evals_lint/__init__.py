"""Static checks for Inspect AI evaluations: structure, tests, best practices and sandbox pinning."""

from inspect_evals_lint.config import (
    PRESETS,
    ConfigError,
    LintConfig,
    find_repo_root,
    load_config,
)
from inspect_evals_lint.context import LintContext, get_all_eval_names, get_all_helper_names
from inspect_evals_lint.diagnostics import (
    Diagnostic,
    Outcome,
    PackageKind,
    PackageReport,
    RunReport,
)
from inspect_evals_lint.registry import CATEGORIES, Rule, get_rule, rule, rules
from inspect_evals_lint.runner import get_all_check_names, lint_evaluation, lint_repository

__version__ = "0.2.1"

__all__ = [
    "CATEGORIES",
    "PRESETS",
    "ConfigError",
    "Diagnostic",
    "LintConfig",
    "LintContext",
    "Outcome",
    "PackageKind",
    "PackageReport",
    "Rule",
    "RunReport",
    "__version__",
    "find_repo_root",
    "get_all_check_names",
    "get_all_eval_names",
    "get_all_helper_names",
    "get_rule",
    "lint_evaluation",
    "lint_repository",
    "load_config",
    "rule",
    "rules",
]
