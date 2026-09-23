"""What rules produce and what a run collects.

A rule yields :class:`Diagnostic` objects, one per site it found something
wrong at, and optionally one :class:`Outcome` saying it passed or did not
apply. A rule that yields nothing passed. The runner stamps each item with
the :class:`~inspect_evals_lint.registry.Rule` that produced it and gathers
them into a :class:`PackageReport`; a whole run is a :class:`RunReport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from inspect_evals_lint.registry import Rule

PackageKind = Literal["eval", "helper"]
"""What a linted package is: an evaluation, or shared code that evaluations import."""

Severity = Literal["error", "warning"]
"""``error`` fails the run; ``warning`` is reported and passes."""

Status = Literal["pass", "fail", "warn", "skip", "suppressed"]

STATUSES: tuple[Status, ...] = ("pass", "fail", "warn", "skip", "suppressed")

RULE_STATUS_ORDER: tuple[Status, ...] = ("fail", "warn", "suppressed", "pass", "skip")
"""Worst first. A rule that reported several things is scored once, at the worst of them.

Suppressed ranks above pass because a suppressed finding counts against the
total: the code still has the problem, the repository has chosen to live with it.
"""

ACTIONABLE_STATUSES: frozenset[Status] = frozenset({"fail", "warn", "suppressed"})
"""Statuses worth showing a reader; ``pass`` and ``skip`` are only counted."""

_SEVERITY_STATUS: dict[str, Status] = {"error": "fail", "warning": "warn"}


@dataclass
class Diagnostic:
    """One finding at one site.

    ``file`` is always set: for something missing it is the path where the
    thing was expected. ``line`` and ``column`` are set when the finding
    points into a file's contents.
    """

    message: str
    file: Path
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    """Last line of a multi-line statement the finding is about, so a suppression comment on any of its lines applies."""
    severity: Severity = "error"
    hint: str | None = None
    """What to do about it, in one sentence."""
    key: str | None = None
    """What an allowlist entry for this finding would name (a role, an image); None for rules without one."""
    rule: Rule | None = None
    """Set by the runner."""
    suppressed: bool = False
    """Set by the runner when a suppression comment or configuration covers the site."""

    @property
    def status(self) -> Status:
        return "suppressed" if self.suppressed else _SEVERITY_STATUS[self.severity]

    @property
    def location(self) -> str:
        text = str(self.file)
        if self.line is not None:
            text += f":{self.line}"
            if self.column is not None:
                text += f":{self.column}"
        return text


@dataclass
class Outcome:
    """A rule's verdict when it has nothing to point at: it passed, or it did not apply."""

    status: Literal["pass", "skip"]
    message: str
    rule: Rule | None = None


Finding = Diagnostic | Outcome
"""What a rule function yields."""


def _rule_name(item: Finding) -> str:
    return item.rule.name if item.rule is not None else "<unregistered>"


@dataclass
class RuleStatus:
    """One rule's verdict on one package: its worst status, with everything it reported."""

    rule: Rule
    status: Status
    outcomes: list[Outcome]
    diagnostics: list[Diagnostic]


@dataclass
class Score:
    """Rules met out of rules applicable, in the terms the register badges use.

    Every rule that ran counts once at its worst status. ``passing`` is ``pass``
    plus ``warn``; ``applicable`` also includes ``fail`` and ``suppressed``;
    ``skip`` is not applicable. ``score`` is ``passing / applicable``, or None when
    nothing applied.
    """

    pass_: int = 0
    fail: int = 0
    warn: int = 0
    skip: int = 0
    suppressed: int = 0
    by_category: dict[str, Score] = field(default_factory=dict)

    def add(self, status: Status) -> None:
        if status == "pass":
            self.pass_ += 1
        else:
            setattr(self, status, getattr(self, status) + 1)

    @property
    def applicable(self) -> int:
        return self.pass_ + self.fail + self.warn + self.suppressed

    @property
    def passing(self) -> int:
        return self.pass_ + self.warn

    @property
    def score(self) -> float | None:
        return round(self.passing / self.applicable, 4) if self.applicable else None

    def to_dict(self, by_category: bool = True) -> dict[str, Any]:
        """The JSON form: the five counts, ``applicable``, ``passing``, ``score`` and, at the top level, ``by_category``."""
        counts: dict[str, Any] = {
            "pass": self.pass_,
            "fail": self.fail,
            "warn": self.warn,
            "skip": self.skip,
            "suppressed": self.suppressed,
            "applicable": self.applicable,
            "passing": self.passing,
            "score": self.score,
        }
        if by_category:
            counts["by_category"] = {
                name: score.to_dict(by_category=False)
                for name, score in sorted(self.by_category.items())
            }
        return counts


def _sort_key(item: Finding) -> tuple[int, str]:
    if item.rule is None:
        return (1, "")
    return (0, item.rule.sort_key)


@dataclass
class PackageReport:
    """Everything one run said about one package."""

    name: str
    kind: PackageKind
    outcomes: list[Outcome] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    skipped: str | None = None
    """Why the package was not linted at all (listed in ``ignore-dirs``); None when it was."""

    def add(self, item: Finding) -> None:
        if isinstance(item, Diagnostic):
            self.diagnostics.append(item)
        else:
            self.outcomes.append(item)

    def passed(self) -> bool:
        return not any(d.status == "fail" for d in self.diagnostics)

    def summary(self) -> dict[Status, int]:
        counts: dict[Status, int] = dict.fromkeys(STATUSES, 0)
        for outcome in self.outcomes:
            counts[outcome.status] += 1
        for diagnostic in self.diagnostics:
            counts[diagnostic.status] += 1
        return counts

    def items(self) -> list[Finding]:
        """Outcomes and diagnostics grouped by rule in execution (code) order.

        Within a rule the outcome comes first, then diagnostics in the order found.
        """
        by_rule: dict[str, list[Finding]] = {}
        for item in (*self.outcomes, *self.diagnostics):
            by_rule.setdefault(_rule_name(item), []).append(item)
        ordered: list[Finding] = []
        for _, group in sorted(by_rule.items(), key=lambda kv: _sort_key(kv[1][0])):
            ordered.extend(sorted(group, key=lambda i: 0 if isinstance(i, Outcome) else 1))
        return ordered

    def statuses(self) -> dict[str, list[Status]]:
        """Rule name to the statuses it produced, outcome first."""
        out: dict[str, list[Status]] = {}
        for item in self.items():
            out.setdefault(_rule_name(item), []).append(item.status)
        return out

    def rules_run(self) -> list[str]:
        return list(dict.fromkeys(_rule_name(i) for i in self.items()))

    def rule_statuses(self) -> list[RuleStatus]:
        """One entry per rule that ran, in execution order, at the worst status it reported."""
        by_rule: dict[str, RuleStatus] = {}
        for item in self.items():
            if item.rule is None:
                continue
            entry = by_rule.get(item.rule.name)
            if entry is None:
                entry = RuleStatus(item.rule, item.status, [], [])
                by_rule[item.rule.name] = entry
            if isinstance(item, Outcome):
                entry.outcomes.append(item)
            else:
                entry.diagnostics.append(item)
            if RULE_STATUS_ORDER.index(item.status) < RULE_STATUS_ORDER.index(entry.status):
                entry.status = item.status
        return list(by_rule.values())

    def score(self) -> Score:
        """Rules met out of rules applicable for this package, overall and by category."""
        score = Score()
        for rule_status in self.rule_statuses():
            score.add(rule_status.status)
            score.by_category.setdefault(rule_status.rule.category, Score()).add(rule_status.status)
        return score


@dataclass
class RunReport:
    """Every package linted in one invocation, with run-wide totals."""

    root: Path
    packages: list[PackageReport] = field(default_factory=list)

    def passed(self) -> bool:
        return all(p.passed() for p in self.packages)

    def summary(self) -> dict[Status, int]:
        totals: dict[Status, int] = dict.fromkeys(STATUSES, 0)
        for package in self.packages:
            for status, count in package.summary().items():
                totals[status] += count
        return totals

    def of_kind(self, kind: PackageKind) -> list[PackageReport]:
        return [p for p in self.packages if p.kind == kind]

    def score(self) -> Score:
        """Rules met out of rules applicable across every package, overall and by category."""
        total = Score()
        for package in self.packages:
            for rule_status in package.rule_statuses():
                total.add(rule_status.status)
                total.by_category.setdefault(rule_status.rule.category, Score()).add(
                    rule_status.status
                )
        return total

    def to_dict(self) -> dict[str, Any]:
        """The JSON document for this run; see docs/output.md."""
        from inspect_evals_lint.render.json import run_to_dict  # renderers import this module

        return run_to_dict(self)
