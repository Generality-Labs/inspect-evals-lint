"""Generated documentation: pages come from the registry, the committed copies are current, and the site builds the rest."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspect_evals_lint import docs
from inspect_evals_lint.registry import get_rule, rules

REPO = Path(__file__).resolve().parents[1]


def test_committed_docs_are_current() -> None:
    """The same check pre-commit runs; regenerate with `python -m inspect_evals_lint.docs`."""
    assert docs.stale_files(REPO) == []


def test_every_rule_has_a_page_with_the_required_sections() -> None:
    for rule in rules():
        page = docs.rule_page(rule)
        assert page.startswith(f"# {rule.code}: {rule.name}")
        assert docs.GENERATED_NOTE in page
        assert "## What it does" in page
        assert "## Why is this bad?" in page
        assert f"ignore[{rule.code}] -- <reason>" in page
        assert "``" not in page.replace("```", "")  # RST double backticks converted to Markdown


def test_allowlist_rules_name_their_table() -> None:
    page = docs.rule_page(get_rule("model_role_resolution"))  # type: ignore[arg-type]
    assert "[tool.inspect-evals-lint.allowlists.model_role_resolution]" in page
    assert "## Options" in page


def test_index_groups_by_category_and_links_pages() -> None:
    index = docs.index_page()
    for heading in (
        "## File structure",
        "## Code quality",
        "## Tests",
        "## Best practices",
        "## Security",
    ):
        assert heading in index
    assert "[IEBP002](rules/IEBP002.md)" in index
    assert "`utils`" in index


def test_config_table_has_a_row_per_field_and_preset_values() -> None:
    table = docs.config_table()
    assert "| `source-root` | `src` | `src/inspect_evals` | `src` |" in table
    assert "| `helper-dirs` |" in table
    assert "| `allowlists.<rule>` |" in table
    assert "| `<rule>` |" in table
    assert "unset" in table  # registry-module in the template preset
    row = next(line for line in table.splitlines() if line.startswith("| `<rule>` |"))
    assert '`{ dockerfile_locking = { host-lock-coupling = "warn" } }`' in row


def test_write_and_check_round_trip(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# x\n\n<!-- config-table:start -->\nold\n<!-- config-table:end -->\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "rules").mkdir(parents=True)
    (tmp_path / "docs" / "rules" / "IEZZ999.md").write_text("stale page", encoding="utf-8")
    assert docs.main(["--check", "--root", str(tmp_path)]) == 1
    assert docs.main(["--root", str(tmp_path)]) == 0
    assert not (tmp_path / "docs" / "rules" / "IEZZ999.md").exists()
    assert (tmp_path / "docs" / "rules" / "IEFS001.md").exists()
    assert "`source-root`" in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert docs.main(["--check", "--root", str(tmp_path)]) == 0


def test_site_index_rewrites_links_for_the_site(tmp_path: Path) -> None:
    (tmp_path / "CHECKS.md").write_text("", encoding="utf-8")
    readme = (
        "# x\n\nSee [rules](docs/CHECKS.md), [pages](docs/rules/), [out](docs/output.md), "
        "[release](RELEASING.md), [ext](https://example.test/a) and [same](#anchor).\n"
    )
    index = docs.site_index(readme, tmp_path)
    assert index.startswith("# x\n")
    assert docs.GENERATED_NOTE in index
    assert "[rules](CHECKS.md)" in index
    assert "[pages](CHECKS.md)" in index
    assert "[out](output.md)" in index
    assert f"[release]({docs.REPO_BLOB}RELEASING.md)" in index
    assert "[ext](https://example.test/a)" in index
    assert "[same](#anchor)" in index


def test_site_builds_the_pages_that_list_every_rule() -> None:
    pages = docs.site_files(REPO)
    assert set(pages) == {"CHECKS.md", "index.md", "llms.txt", "llms-full.txt", "rules.json"}
    assert docs.GENERATED_NOTE in pages["index.md"]
    for path in pages:
        if path != "CHECKS.md":
            assert not (REPO / "docs" / path).exists(), f"docs/{path} is built with the site"


def test_committed_rule_index_points_at_the_site() -> None:
    """Releases up to 0.10.0 print a link to docs/CHECKS.md on GitHub."""
    pointer = (REPO / "docs" / "CHECKS.md").read_text(encoding="utf-8")
    assert f"{docs.SITE_URL}CHECKS/" in pointer
    assert "rules/IEFS001.md" not in pointer
    assert "## Helper packages" in pointer  # inspect_evals links to this anchor


def test_site_build_replaces_the_pointer_with_the_generated_pages(tmp_path: Path) -> None:
    """Runs where the docs group is installed: docs.yml, or `uv run --group docs pytest`."""
    pytest.importorskip("mkdocs")
    from mkdocs.commands.build import build
    from mkdocs.config import load_config  # pyright: ignore[reportUnknownVariableType]

    build(load_config(config_file=str(REPO / "mkdocs.yml"), site_dir=str(tmp_path)))
    for path, content in docs.site_files(REPO).items():
        assert (tmp_path / path).read_text(encoding="utf-8") == content
    assert (tmp_path / "CHECKS" / "index.md").read_text(encoding="utf-8") == docs.formatted(
        docs.index_page()
    )


def test_site_url_matches_mkdocs() -> None:
    import yaml

    config = yaml.safe_load((REPO / "mkdocs.yml").read_text(encoding="utf-8"))
    assert config["site_url"] == docs.SITE_URL


def test_llms_txt_lists_every_rule_as_raw_markdown() -> None:
    text = docs.llms_txt()
    assert text.startswith("# inspect-evals-lint\n\n> ")
    for rule in rules():
        assert f"[{rule.code} {rule.name}]({docs.SITE_URL}rules/{rule.code}.md)" in text
    assert f"{docs.SITE_URL}rules.json" in text
    assert "llms-full.txt" in text


def test_rules_json_is_complete_and_machine_readable() -> None:
    import json

    data = json.loads(docs.rules_json())
    assert data["site"] == docs.SITE_URL
    by_code = {r["code"]: r for r in data["rules"]}
    assert set(by_code) == {r.code for r in rules()}
    entry = by_code["IEBP002"]
    assert entry["name"] == "model_role_resolution"
    assert entry["allowlist"] is True
    assert entry["scopes"] == ["eval", "helper"]
    assert entry["markdown"].endswith("/rules/IEBP002.md")


def test_llms_full_concatenates_every_page() -> None:
    text = docs.llms_full_txt(REPO, (REPO / "README.md").read_text(encoding="utf-8"))
    for rule in rules():
        assert f"# {rule.code}: {rule.name}" in text
    assert "# Rule index" in text
    assert "# Output formats" in text
    assert docs.GENERATED_NOTE not in text


def test_post_page_hook_publishes_raw_markdown(tmp_path: Path) -> None:
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("mkdocs_hooks", REPO / "mkdocs_hooks.py")
    assert spec
    assert spec.loader
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    config = {"site_dir": str(tmp_path)}
    for path, content in (
        ("index.md", "# home\n"),
        ("CHECKS.md", "# rules\n"),
        ("rules/IEFS001.md", "# IEFS001\n"),
    ):
        page = SimpleNamespace(file=SimpleNamespace(src_uri=path, content_string=content))
        assert hooks.on_post_page("<html>", page, config) == "<html>"
    assert (tmp_path / "index.md").read_text() == "# home\n"
    assert not (tmp_path / "index" / "index.md").exists()
    assert (tmp_path / "CHECKS.md").read_text() == "# rules\n"
    assert (tmp_path / "CHECKS" / "index.md").read_text() == "# rules\n"
    assert (tmp_path / "rules" / "IEFS001.md").read_text() == "# IEFS001\n"
    assert (tmp_path / "rules" / "IEFS001" / "index.md").read_text() == "# IEFS001\n"
