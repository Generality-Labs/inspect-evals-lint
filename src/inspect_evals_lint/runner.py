"""Per-package orchestration: which rules run, in what order, and how findings are gathered."""

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
from inspect_evals_lint.diagnostics import Diagnostic, Outcome, PackageReport, RunReport
from inspect_evals_lint.registry import Rule, get_rule, rule_names, rules
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions

LOCATION_RULE = "eval_location"
"""The rule that establishes the package exists. Nothing else runs when it does not."""


def get_all_check_names() -> list[str]:
    """Every rule name, sorted."""
    return rule_names()


def _selected(rule: Rule, only: Rule | None, config: LintConfig) -> bool:
    if only is not None and rule is not only:
        return False
    return rule.name not in config.disabled_checks


def _run_rule(rule: Rule, context: LintContext, report: PackageReport) -> None:
    findings = list(rule.run(context))
    for finding in findings:
        finding.rule = rule
        report.add(finding)
    if not findings:
        report.add(Outcome("pass", rule.summary, rule=rule))


def lint_evaluation(
    repo_root: Path,
    eval_name: str,
    config: LintConfig | None = None,
    check: str | None = None,
) -> PackageReport:
    """Run every enabled rule against one package and return its report.

    Args:
        repo_root: Repository root.
        eval_name: Directory name of the evaluation or helper package under ``config.source_root``.
        config: Layout configuration; loaded from ``repo_root/pyproject.toml`` when omitted.
        check: Run only this rule, by name or code.

    Raises:
        ValueError: ``check`` names no rule.
    """
    config = config or load_config(repo_root)
    kind = package_kind(eval_name, config)
    report = PackageReport(name=eval_name, kind=kind)

    if eval_name in config.ignore_dirs:
        report.skipped = f"'{eval_name}' is listed in ignore-dirs"
        return report

    only: Rule | None = None
    if check is not None:
        only = get_rule(check)
        if only is None:
            raise ValueError(f"Unknown check: '{check}'. Available checks: {rule_names()}")
        if not only.applies_to(kind):
            report.add(
                Outcome("skip", f"'{only.name}' does not apply to a {kind} package", rule=only)
            )
            return report

    context = LintContext.build(repo_root, eval_name, config)
    for rule in rules():
        if rule.applies_to(kind) and _selected(rule, only, config):
            _run_rule(rule, context, report)
        if rule.name == LOCATION_RULE and not is_package(context.path):
            return report

    apply_suppressions(report.diagnostics, load_suppressions(context.path))
    return report


def lint_repository(
    repo_root: Path,
    config: LintConfig | None = None,
    names: list[str] | None = None,
    check: str | None = None,
) -> RunReport:
    """Lint every evaluation and helper package in the repository (or just ``names``)."""
    config = config or load_config(repo_root)
    if names is None:
        names = [*get_all_eval_names(repo_root, config), *get_all_helper_names(repo_root, config)]
    return RunReport(
        root=repo_root,
        packages=[lint_evaluation(repo_root, name, config, check=check) for name in names],
    )


__all__ = [
    "LOCATION_RULE",
    "Diagnostic",
    "get_all_check_names",
    "get_all_eval_names",
    "get_all_helper_names",
    "lint_evaluation",
    "lint_repository",
    "package_kind",
]
