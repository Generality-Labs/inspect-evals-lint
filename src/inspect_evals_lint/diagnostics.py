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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from inspect_evals_lint.registry import Rule

PackageKind = Literal["eval", "helper"]
"""What a linted package is: an evaluation, or shared code that evaluations import."""

Severity = Literal["error", "warning"]
"""``error`` fails the run; ``warning`` is reported and passes."""

Status = Literal["pass", "fail", "warn", "skip", "suppressed"]

STATUSES: tuple[Status, ...] = ("pass", "fail", "warn", "skip", "suppressed")

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
    severity: Severity = "error"
    hint: str | None = None
    """What to do about it, in one sentence."""
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
