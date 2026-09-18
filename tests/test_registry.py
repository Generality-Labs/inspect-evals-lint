"""The rule registry's invariants: one declaration per rule, and every declaration consistent."""

from __future__ import annotations

import pytest

from inspect_evals_lint.registry import (
    CATEGORIES,
    CATEGORY_PREFIX,
    RegistrationError,
    Rule,
    get_rule,
    rule,
    rules,
)


def test_codes_and_names_are_unique() -> None:
    all_rules = rules()
    assert len({r.code for r in all_rules}) == len(all_rules)
    assert len({r.name for r in all_rules}) == len(all_rules)


def test_codes_carry_their_category_prefix() -> None:
    for r in rules():
        assert r.code.startswith(CATEGORY_PREFIX[r.category]), r.name
        assert r.category in CATEGORIES


def test_rules_run_in_category_then_code_order() -> None:
    order = [(CATEGORIES.index(r.category), r.code) for r in rules()]
    assert order == sorted(order)
    assert rules()[0].name == "package_location"


def test_every_rule_is_documented() -> None:
    for r in rules():
        assert r.summary, r.name
        assert r.doc, f"{r.name} has no docstring"


def test_lookup_by_name_or_code() -> None:
    by_name = get_rule("readme")
    assert isinstance(by_name, Rule)
    assert get_rule(by_name.code) is by_name
    assert get_rule("nope") is None


def _noop(ctx):  # pragma: no cover - never run
    return []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"code": "FS001", "name": "x", "category": "file_structure", "summary": "s"}, "look like"),
        ({"code": "IEBP999", "name": "x", "category": "file_structure", "summary": "s"}, "prefix"),
        (
            {"code": "IEFS999", "name": "readme", "category": "file_structure", "summary": "s"},
            "already",
        ),
        ({"code": "IEFS006", "name": "x", "category": "file_structure", "summary": "s"}, "already"),
        (
            {
                "code": "IEFS999",
                "name": "x",
                "category": "file_structure",
                "summary": "s",
                "scopes": ("helper",),
            },
            "evaluations",
        ),
        (
            {"code": "IEFS999", "name": "x", "category": "file_structure", "summary": "  "},
            "summary",
        ),
    ],
)
def test_inconsistent_declarations_are_rejected(kwargs: dict[str, object], match: str) -> None:
    rules()  # make sure the real rules are loaded so duplicates are detectable
    with pytest.raises(RegistrationError, match=match):
        rule(**kwargs)(_noop)  # type: ignore[arg-type]
