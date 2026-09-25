"""Static checks for Inspect AI evaluations: structure, tests, best practices and sandbox pinning."""

from inspect_evals_lint.config import (
    DEFAULT_PRESET,
    PRESETS,
    SINGLE_EVAL_PRESET,
    ConfigError,
    LintConfig,
    find_repo_root,
    infer_preset,
    load_config,
)
from inspect_evals_lint.context import (
    LintContext,
    TaskLayout,
    UnsupportedLayoutError,
    evaluation_names,
    helper_names,
    task_layout,
    task_layouts,
)
from inspect_evals_lint.diagnostics import (
    RULE_STATUS_ORDER,
    Diagnostic,
    Outcome,
    PackageKind,
    PackageReport,
    RuleStatus,
    RunReport,
    Score,
)
from inspect_evals_lint.registry import (
    CATEGORIES,
    Reference,
    Rule,
    get_rule,
    rule,
    rule_names,
    rules,
)
from inspect_evals_lint.runner import lint_package, lint_repository, lint_task_files

__version__ = "0.7.0"

__all__ = [
    "CATEGORIES",
    "DEFAULT_PRESET",
    "PRESETS",
    "RULE_STATUS_ORDER",
    "SINGLE_EVAL_PRESET",
    "ConfigError",
    "Diagnostic",
    "LintConfig",
    "LintContext",
    "Outcome",
    "PackageKind",
    "PackageReport",
    "Reference",
    "Rule",
    "RuleStatus",
    "RunReport",
    "Score",
    "TaskLayout",
    "UnsupportedLayoutError",
    "__version__",
    "evaluation_names",
    "find_repo_root",
    "get_rule",
    "helper_names",
    "infer_preset",
    "lint_package",
    "lint_repository",
    "lint_task_files",
    "load_config",
    "rule",
    "rule_names",
    "rules",
    "task_layout",
    "task_layouts",
]
