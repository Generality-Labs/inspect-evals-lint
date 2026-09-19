"""Generated documentation: pages come from the registry and the committed copies are current."""

from __future__ import annotations

from pathlib import Path

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
        assert f"ignore[{rule.code}]" in page
        assert "``" not in page.replace("```", "")  # RST double backticks converted to Markdown


def test_allowlist_rules_name_their_table() -> None:
    page = docs.rule_page(get_rule("model_role_resolution"))  # type: ignore[arg-type]
    assert "[tool.inspect-evals-lint.allowlists.model_role_resolution]" in page
    assert "## Options" in page


def test_index_groups_by_category_and_links_pages() -> None:
    index = docs.index_page()
    for heading in ("## File structure", "## Code quality", "## Tests", "## Best practices"):
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


def test_committed_site_index_matches_readme() -> None:
    assert (REPO / "docs" / "index.md").exists()
    assert docs.stale_files(REPO) == []
