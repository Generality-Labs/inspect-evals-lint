"""Per-package orchestration: which rules run, in what order, and how findings are gathered."""

from __future__ import annotations

from pathlib import Path

from inspect_evals_lint.config import LintConfig, load_config
from inspect_evals_lint.context import (
    LintContext,
    evaluation_names,
    helper_names,
    is_package,
    package_kind,
    task_layouts,
)
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome, PackageReport, RunReport
from inspect_evals_lint.registry import Rule, get_rule, rule_names, rules
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions

LOCATION_RULE = "package_location"
"""The rule that establishes the package exists. Nothing else runs when it does not."""


def _selected(rule: Rule, only: Rule | None, config: LintConfig) -> bool:
    if only is not None:
        return rule is only
    return config.selects(rule)


def _run_rule(rule: Rule, context: LintContext, report: PackageReport) -> None:
    findings = list(rule.run(context))
    for finding in findings:
        finding.rule = rule
    if rule.allowlist:
        findings.extend(_apply_allowlist(rule, context, findings))
    if any(isinstance(f, Diagnostic) for f in findings):
        # A rule cannot both pass and point at something; a skip can stand beside a warning.
        findings = [f for f in findings if not (isinstance(f, Outcome) and f.status == "pass")]
    for finding in findings:
        report.add(finding)
    if not findings:
        report.add(Outcome("pass", rule.summary, rule=rule))


def _apply_allowlist(rule: Rule, context: LintContext, findings: list[Finding]) -> list[Diagnostic]:
    """Turn allowlisted failures into warnings; return warnings for entries nothing matched.

    The ratchet: an existing surface can be burned down while new violations are
    blocked, and a stale entry is reported so it gets removed.
    """
    allowed = context.config.allowlist_for(rule, context.name)
    seen: set[str] = set()
    for finding in findings:
        if isinstance(finding, Diagnostic) and finding.key is not None and finding.key in allowed:
            seen.add(finding.key)
            finding.severity = "warning"
            finding.message = f"Allowlisted: {finding.message}"
            finding.hint = (
                f"{finding.hint}, then remove the allowlist entry"
                if finding.hint
                else "remove the allowlist entry once fixed"
            )
    return [
        Diagnostic(
            f"Allowlist entry {key!r} for {rule.name} on {context.name!r} is no longer needed",
            file=context.root / "pyproject.toml",
            severity="warning",
            hint=f"remove it from [tool.inspect-evals-lint.allowlists.{rule.name}]",
            key=key,
            rule=rule,
        )
        for key in sorted(allowed - seen)
    ]


def lint_package(
    repo_root: Path,
    name: str,
    config: LintConfig | None = None,
    check: str | None = None,
) -> PackageReport:
    """Run every enabled rule against one package and return its report.

    Args:
        repo_root: Repository root.
        name: Directory name of the evaluation or helper package under ``config.source_root``.
        config: Layout configuration; loaded from ``repo_root/pyproject.toml`` when omitted.
        check: Run only this rule, by name or code.

    Raises:
        ValueError: ``check`` names no rule.
    """
    config = config or load_config(repo_root)
    kind = package_kind(name, config)
    report = PackageReport(name=name, kind=kind)

    if name in config.ignore_dirs:
        report.skipped = f"'{name}' is listed in ignore-dirs"
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

    context = LintContext.build(repo_root, name, config)
    for rule in rules():
        if rule.applies_to(kind) and _selected(rule, only, config):
            _run_rule(rule, context, report)
        if rule.name == LOCATION_RULE and not is_package(context.path):
            return report

    apply_suppressions(report.diagnostics, load_suppressions(context), config, repo_root)
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
        names = [*evaluation_names(repo_root, config), *helper_names(repo_root, config)]
    return RunReport(
        root=repo_root,
        packages=[lint_package(repo_root, name, config, check=check) for name in names],
    )


def lint_task_files(
    repo_root: Path,
    task_paths: list[str],
    config: LintConfig | None = None,
    check: str | None = None,
) -> RunReport:
    """Lint the package holding each task file, one package per distinct layout.

    This is how a register entry is linted: the entry names task files, not
    packages, and the repository's own configuration is not consulted. ``config``
    supplies everything but the layout, which each task file determines
    (``REGISTER_CONFIG`` when omitted: the single-evaluation layout with
    ``eval.yaml`` optional, since the register entry holds the metadata).

    Raises:
        UnsupportedLayoutError: a task file is missing, escapes the repository, or is a bare module.
    """
    from inspect_evals_lint.config import REGISTER_CONFIG

    base = config or REGISTER_CONFIG
    packages = [
        lint_package(repo_root, layout.eval_name, layout.config(base), check=check)
        for layout in task_layouts(repo_root, task_paths)
    ]
    return RunReport(root=repo_root, packages=packages)


__all__ = [
    "LOCATION_RULE",
    "Diagnostic",
    "evaluation_names",
    "helper_names",
    "lint_package",
    "lint_repository",
    "lint_task_files",
    "package_kind",
]
