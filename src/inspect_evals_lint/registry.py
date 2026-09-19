"""Rules as data: each check declares itself once, next to its implementation.

A rule is a function taking a :class:`LintContext` and yielding results,
decorated with :func:`rule`, which records its code, name, category, scopes
and one-line summary. The registry is the single source of truth for what
exists, in what order it runs, and what the documentation says.
"""

from __future__ import annotations

import importlib
import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, get_args

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.models import LintResult, PackageKind

Category = Literal["file_structure", "code_quality", "tests", "best_practices"]
CATEGORIES: tuple[str, ...] = get_args(Category)
"""The four sections of the documentation. Downstream badge tooling groups by exactly these."""

CATEGORY_PREFIX: dict[str, str] = {
    "file_structure": "IEFS",
    "code_quality": "IECQ",
    "tests": "IETS",
    "best_practices": "IEBP",
}
"""Rule code prefix per category. ``IE`` namespaces the codes away from ruff's."""

_CODE_PATTERN = re.compile(r"^IE(FS|CQ|TS|BP)\d{3}$")

RuleFn = Callable[[LintContext], Iterable[LintResult]]


class RegistrationError(ValueError):
    """A rule declaration is inconsistent with the registry's invariants."""


@dataclass(frozen=True)
class Rule:
    """One check: its identity, where it applies, and how to run it."""

    code: str
    name: str
    category: Category
    scopes: frozenset[PackageKind]
    summary: str
    run: RuleFn
    allowlist: bool = False
    """Whether the rule reads ``[tool.inspect-evals-lint.allowlists.<name>]``."""

    @property
    def doc(self) -> str:
        """The rule's documentation: the docstring of its implementation."""
        return inspect.getdoc(self.run) or ""

    def applies_to(self, kind: PackageKind) -> bool:
        return kind in self.scopes


_RULES: dict[str, Rule] = {}
_BY_CODE: dict[str, Rule] = {}


def rule(
    *,
    code: str,
    name: str,
    category: Category,
    scopes: Iterable[PackageKind] = ("eval",),
    summary: str,
    allowlist: bool = False,
) -> Callable[[RuleFn], RuleFn]:
    """Register the decorated function as a rule. Returns the function unchanged."""

    def register(fn: RuleFn) -> RuleFn:
        if not _CODE_PATTERN.match(code):
            raise RegistrationError(f"{name}: code {code!r} must look like IEFS001")
        if not code.startswith(CATEGORY_PREFIX[category]):
            raise RegistrationError(
                f"{name}: code {code!r} does not carry the {category} prefix "
                f"{CATEGORY_PREFIX[category]!r}"
            )
        if name in _RULES:
            raise RegistrationError(f"rule name {name!r} is already registered")
        if code in _BY_CODE:
            raise RegistrationError(f"rule code {code!r} is already registered")
        scope_set = frozenset(scopes)
        if "eval" not in scope_set:
            raise RegistrationError(f"{name}: every rule applies to evaluations")
        if not summary.strip():
            raise RegistrationError(f"{name}: a summary is required")
        entry = Rule(
            code=code,
            name=name,
            category=category,
            scopes=scope_set,
            summary=summary.strip(),
            run=fn,
            allowlist=allowlist,
        )
        _RULES[name] = entry
        _BY_CODE[code] = entry
        return fn

    return register


def _ensure_loaded() -> None:
    # Importing the rules package registers every rule as a side effect.
    importlib.import_module("inspect_evals_lint.rules")


def rules() -> list[Rule]:
    """Every rule, in execution order: by category, then by code.

    Codes therefore define the order results are reported in, and a rule that
    relies on an earlier one having reported (``init_exports`` after
    ``main_file``) expresses that by its number.
    """
    _ensure_loaded()
    return sorted(_RULES.values(), key=lambda r: (CATEGORIES.index(r.category), r.code))


def rule_names() -> list[str]:
    """Every rule name, sorted."""
    return sorted(r.name for r in rules())


def get_rule(name_or_code: str) -> Rule | None:
    """Look a rule up by name or code; None when unknown."""
    _ensure_loaded()
    return _RULES.get(name_or_code) or _BY_CODE.get(name_or_code)


def category_of(name: str) -> str | None:
    """The category a rule belongs to; None for runner-level results such as ``invalid_check``."""
    found = get_rule(name)
    return found.category if found else None
