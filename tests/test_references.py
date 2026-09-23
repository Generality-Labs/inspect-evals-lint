"""Rule references: where they may point, how they render, and (on request) that they resolve."""

from __future__ import annotations

import os
import re
import urllib.request

import pytest

from inspect_evals_lint.registry import (
    INSPECT_DOCS,
    REFERENCE_SITES,
    Reference,
    RegistrationError,
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
