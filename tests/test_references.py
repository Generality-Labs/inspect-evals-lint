"""Rule references: where they may point, how they render, and (on request) that they resolve."""

from __future__ import annotations

import json
import os
import re
import urllib.request

import pytest

from inspect_evals_lint import docs
from inspect_evals_lint.registry import (
    INSPECT_DOCS,
    REFERENCE_SITES,
    Reference,
    RegistrationError,
    get_rule,
    inspect_docs,
    rule,
    rules,
)


def _documented(ctx):  # pragma: no cover - never run
    """A stand-in rule.

    ## What it does
    Nothing.

    ## Why is this bad?
    It is not.
    """
    return []


def test_inspect_docs_builds_a_page_url_with_an_optional_anchor() -> None:
    assert inspect_docs("tasks", "Tasks: Parameters", "parameters") == Reference(
        "Tasks: Parameters", f"{INSPECT_DOCS}tasks.html#parameters"
    )
    assert (
        inspect_docs("custom-scorers", "Custom Scorers").url == f"{INSPECT_DOCS}custom-scorers.html"
    )


@pytest.mark.parametrize(
    ("references", "match"),
    [
        ((Reference("Elsewhere", "https://example.com/page"),), "not under one of"),
        ((Reference("", f"{INSPECT_DOCS}tasks.html"),), "needs a title"),
        (
            (inspect_docs("tasks", "A", "x"), inspect_docs("tasks", "B", "x")),
            "listed twice",
        ),
    ],
)
def test_references_outside_the_known_sites_or_malformed_are_rejected(
    references: tuple[Reference, ...], match: str
) -> None:
    rules()
    with pytest.raises(RegistrationError, match=match):
        rule(
            code="IEFS997",
            name="badly_referenced",
            category="file_structure",
            summary="s",
            references=references,
        )(_documented)  # type: ignore[arg-type]


def test_every_reference_is_under_a_known_site_and_titled() -> None:
    for r in rules():
        for ref in r.references:
            assert ref.url.startswith(REFERENCE_SITES), (r.name, ref.url)
            assert ref.title.strip(), (r.name, ref.url)


def test_the_conventions_inspect_documents_are_referenced() -> None:
    """The rules whose convention has a home in the Inspect docs point at it; the linter-internal ones do not."""
    referenced = {r.code for r in rules() if r.references}
    assert {
        "IEBP001",
        "IEBP002",
        "IEBP003",
        "IEBP004",
        "IECQ002",
        "IECQ003",
        "IETS004",
    } <= referenced
    assert {"IECQ005", "IEFS005", "IEFS006", "IETS002"}.isdisjoint(referenced)
    role = get_rule("model_role_resolution")
    assert role
    assert f"{INSPECT_DOCS}models.html#role-defaults" in {ref.url for ref in role.references}


def test_rule_page_lists_references_under_see_also_before_the_suppression_line() -> None:
    page = docs.rule_page(get_rule("sample_ids"))  # type: ignore[arg-type]
    assert "## See also" in page
    assert f"- [Datasets: Dataset Samples]({INSPECT_DOCS}datasets.html#dataset-samples)" in page
    assert page.index("## See also") < page.index("Suppress on a line with")
    unreferenced = docs.rule_page(get_rule("suppression_syntax"))  # type: ignore[arg-type]
    assert "## See also" not in unreferenced


def test_rules_json_and_list_rules_carry_references() -> None:
    from inspect_evals_lint.cli import _rule_dict

    data = json.loads(docs.rules_json())
    entry = next(r for r in data["rules"] if r["code"] == "IEBP002")
    assert {
        "title": "Models: Role Defaults",
        "url": f"{INSPECT_DOCS}models.html#role-defaults",
    } in entry["references"]
    assert _rule_dict(get_rule("IEBP002"))["references"] == entry["references"]  # type: ignore[arg-type]
    assert next(r for r in data["rules"] if r["code"] == "IECQ005")["references"] == []


@pytest.mark.skipif(
    not os.environ.get("INSPECT_EVALS_LINT_CHECK_LINKS"),
    reason="fetches the referenced documentation; set INSPECT_EVALS_LINT_CHECK_LINKS=1 to run",
)
def test_every_reference_resolves_and_its_anchor_exists() -> None:
    """Fetch each referenced page once and check the section anchor is on it; the docs workflow runs this."""
    pages: dict[str, str] = {}
    problems: list[str] = []
    for r in rules():
        for ref in r.references:
            url, _, anchor = ref.url.partition("#")
            if url not in pages:
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(
                            url, headers={"User-Agent": "inspect-evals-lint link check"}
                        ),
                        timeout=30,
                    ) as response:
                        pages[url] = response.read().decode("utf-8", errors="replace")
                except Exception as e:
                    pages[url] = ""
                    problems.append(f"{r.code}: {url} failed to fetch ({e})")
                    continue
            if anchor and not re.search(rf'id="{re.escape(anchor)}"', pages[url]):
                problems.append(f"{r.code}: no element with id={anchor!r} on {url}")
    assert not problems, "\n".join(problems)
