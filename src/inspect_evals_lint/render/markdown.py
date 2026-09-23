"""A Markdown summary of a run: per package, a headline count and the rules that need attention.

This is the shape a pull-request comment or a job summary wants: one line
saying how many rules are met, the per-category split, and a collapsible
list of the rules not met, with warnings or suppressed, each finding on its
own line with the hint under it. Rules that passed or did not apply are
counted but not listed. Every piece of text is escaped so that a message or
path from the linted repository cannot inject markup or break a table.

Locations are code spans by default; pass ``source_link`` to turn them into
links, for example into a GitHub blob URL at the linted commit.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from inspect_evals_lint.diagnostics import (
    ACTIONABLE_STATUSES,
    Diagnostic,
    PackageReport,
    RunReport,
    Score,
)
from inspect_evals_lint.registry import CATEGORIES
from inspect_evals_lint.render.paths import relative_to_root

RULE_DOCS_BASE = "https://inspect-evals-lint.generality.org/rules"
CATEGORY_LABELS: dict[str, str] = {
    "file_structure": "Structure",
    "code_quality": "Code quality",
    "tests": "Tests",
    "best_practices": "Best practices",
}
STATUS_LABELS: dict[str, str] = {
    "fail": "Not met",
    "warn": "Warning",
    "suppressed": "Suppressed",
    "pass": "Met",
    "skip": "Not applicable",
}

SourceLink = Callable[[str, int | None], str | None]
"""Maps a root-relative file path and optional line to a URL, or None for no link."""


def code(text: str) -> str:
    """An inline code span that cannot end early or break a table cell."""
    return "`" + text.replace("`", "'").replace("|", "\\|").replace("\n", " ") + "`"


def plain(text: str) -> str:
    """Prose on one line with table pipes escaped; Markdown in the text stays literal enough."""
    return " ".join(text.split()).replace("|", "\\|")


def counts(score: Score | None) -> str:
    return f"{score.passing}/{score.applicable}" if score and score.applicable else "—"


def rule_link(code_: str, name: str, docs_base: str = RULE_DOCS_BASE) -> str:
    return f"[{code(name)}]({docs_base}/{code_}/)"


def location(diagnostic: Diagnostic, root: Path | None, source_link: SourceLink | None) -> str:
    """The finding's location as a code span, linked when ``source_link`` gives a URL."""
    path = relative_to_root(diagnostic.file, root)
    label = f"{path}:{diagnostic.line}" if diagnostic.line else path
    url = source_link(path, diagnostic.line) if source_link else None
    return f"[{code(label)}]({url})" if url else code(label)


def headline(package: PackageReport) -> str:
    """``**14/17 checks met** · Structure 6/6 · ...`` for one package."""
    score = package.score()
    parts = [f"**{counts(score)} checks met**"]
    for category in CATEGORIES:
        parts.append(f"{CATEGORY_LABELS[category]} {counts(score.by_category.get(category))}")
    return " · ".join(parts)


def package_markdown(
    package: PackageReport,
    root: Path | None = None,
    *,
    source_link: SourceLink | None = None,
    docs_base: str = RULE_DOCS_BASE,
    heading_level: int = 3,
    heading: str | None = None,
) -> list[str]:
    """The Markdown lines for one package, starting with a heading."""
    title = heading if heading is not None else code(package.name)
    if package.kind == "helper" and heading is None:
        title += " (helper package)"
    lines = ["#" * heading_level + " " + title, ""]
    if package.skipped:
        return [*lines, f"Not linted: {plain(package.skipped)}", ""]
    lines += [headline(package), ""]

    actionable: list[str] = []
    for rule_status in package.rule_statuses():
        if rule_status.status not in ACTIONABLE_STATUSES:
            continue
        rule = rule_status.rule
        actionable.append(
            f"- **{STATUS_LABELS[rule_status.status]}** {rule_link(rule.code, rule.name, docs_base)}"
        )
        for diagnostic in rule_status.diagnostics:
            prefix = (
                "*suppressed* "
                if diagnostic.status == "suppressed" and rule_status.status != "suppressed"
                else ""
            )
            text = (
                f"  - {prefix}{location(diagnostic, root, source_link)} {plain(diagnostic.message)}"
            )
            if diagnostic.hint:
                text += f"<br>  Hint: {plain(diagnostic.hint)}"
            actionable.append(text)
    if not actionable:
        return [*lines, "Every applicable check is met.", ""]
    rules_listed = sum(1 for line in actionable if line.startswith("- **"))
    return [
        *lines,
        "<details>",
        f"<summary>{rules_listed} rule(s) not met, with warnings or suppressed</summary>",
        "",
        *actionable,
        "",
        "</details>",
        "",
    ]


def render_markdown(
    run: RunReport,
    *,
    source_link: SourceLink | None = None,
    docs_base: str = RULE_DOCS_BASE,
    heading_level: int = 3,
    footer: bool = True,
) -> str:
    """The Markdown document for a run: every package, then one line on how counts work."""
    lines: list[str] = []
    for package in run.packages:
        lines.extend(
            package_markdown(
                package,
                run.root,
                source_link=source_link,
                docs_base=docs_base,
                heading_level=heading_level,
            )
        )
    if footer:
        lines.append(
            "Counts are rules met out of applicable rules: warnings count as met, suppressed "
            "findings count against the total, and rules that do not apply are excluded. "
            "Rule names link to their documentation."
        )
    return "\n".join(lines).rstrip("\n") + "\n"
