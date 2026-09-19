"""Per-package orchestration: which rules run, in what order, and how results are gathered."""

from __future__ import annotations

from pathlib import Path

from inspect_evals_lint.config import LintConfig, load_config
from inspect_evals_lint.context import (
    LintContext,
    get_all_eval_names,
    get_all_helper_names,
    is_package,
    package_kind,
)
from inspect_evals_lint.models import LintReport, LintResult
from inspect_evals_lint.registry import (
    CATEGORIES,
    Rule,
    category_of,
    get_rule,
    rule_names,
    rules,
)
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions

__all__ = [
    "CATEGORIES",
    "LOCATION_RULE",
    "category_of",
    "get_all_check_names",
    "get_all_eval_names",
    "get_all_helper_names",
    "lint_evaluation",
    "package_kind",
]

LOCATION_RULE = "eval_location"
"""The rule that establishes the package exists. Nothing else runs when it does not."""


def get_all_check_names() -> list[str]:
    """Every rule name, sorted."""
    return rule_names()


def _selected(rule: Rule, only: str | None, config: LintConfig) -> bool:
    if only is not None and only not in (rule.name, rule.code):
        return False
    return rule.name not in config.disabled_checks


def lint_evaluation(
    repo_root: Path,
    eval_name: str,
    config: LintConfig | None = None,
    check: str | None = None,
) -> LintReport:
    """Run every enabled rule against one package and return its report.

    Args:
        repo_root: Repository root.
        eval_name: Directory name of the evaluation or helper package under ``config.source_root``.
        config: Layout configuration; loaded from ``repo_root/pyproject.toml`` when omitted.
        check: Run only this rule, by name or code.
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

    if check is not None:
        selected = get_rule(check)
        if selected is None:
            report.add(
                LintResult(
                    name="invalid_check",
                    status="fail",
                    message=f"Unknown check: '{check}'. Available checks: {rule_names()}",
                )
            )
            return report
        if not selected.applies_to(kind):
            report.add(
                LintResult(
                    name=selected.name,
                    status="skip",
                    message=f"'{selected.name}' does not apply to a {kind} package",
                )
            )
            return report

    context = LintContext.build(repo_root, eval_name, config)
    for rule in rules():
        if not rule.applies_to(kind) or not _selected(rule, check, config):
            if rule.name == LOCATION_RULE and not is_package(context.path):
                return report
            continue
        report.results.extend(rule.run(context))
        if rule.name == LOCATION_RULE and not is_package(context.path):
            return report

    apply_suppressions(report.results, load_suppressions(context.path))
    return report
