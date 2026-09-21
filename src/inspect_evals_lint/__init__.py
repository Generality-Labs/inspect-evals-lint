"""Static checks for Inspect AI evaluations: structure, tests, best practices and sandbox pinning."""

from inspect_evals_lint.config import (
    PRESETS,
    ConfigError,
    LintConfig,
    find_repo_root,
    load_config,
)
from inspect_evals_lint.context import LintContext, evaluation_names, helper_names
from inspect_evals_lint.diagnostics import (
    Diagnostic,
    Outcome,
    PackageKind,
    PackageReport,
    RunReport,
)
from inspect_evals_lint.registry import CATEGORIES, Rule, get_rule, rule, rule_names, rules
from inspect_evals_lint.runner import lint_package, lint_repository

__version__ = "0.4.2"

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
    "evaluation_names",
    "find_repo_root",
    "get_rule",
    "helper_names",
    "lint_package",
    "lint_repository",
    "load_config",
    "rule",
    "rule_names",
    "rules",
]
