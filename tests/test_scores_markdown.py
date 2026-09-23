"""Per-rule scores and the Markdown renderer."""

from __future__ import annotations

from pathlib import Path

from inspect_evals_lint import LintConfig, RunReport, lint_package
from inspect_evals_lint.diagnostics import Diagnostic, Outcome, PackageReport
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.render import package_markdown, render_json, render_markdown
from inspect_evals_lint.render.markdown import code, plain
from tests.conftest import write


def report_with(*items: tuple[str, str, str | None]) -> PackageReport:
    """A package report from (rule name, status, message) triples."""
    report = PackageReport(name="alpha", kind="eval")
    for name, status, message in items:
        rule = get_rule(name)
        assert rule
        if status in ("pass", "skip"):
            report.add(Outcome(status, message or f"{name} {status}", rule=rule))  # type: ignore[arg-type]
        else:
            report.add(
                Diagnostic(
                    message or f"{name} {status}",
                    file=Path("/repo/src/alpha/alpha.py"),
                    line=7,
                    severity="warning" if status == "warn" else "error",
                    hint="do the thing",
                    rule=rule,
                    suppressed=status == "suppressed",
                )
            )
    return report


def test_rule_statuses_take_the_worst_status_per_rule() -> None:
    report = report_with(
        ("sample_ids", "fail", None),
        ("sample_ids", "suppressed", None),
        ("readme", "pass", None),
        ("readme", "warn", "TODO"),
        ("e2e_test", "suppressed", None),
        ("sandbox_image_pinning", "skip", None),
    )
    by_rule = {rs.rule.name: rs for rs in report.rule_statuses()}
    assert by_rule["sample_ids"].status == "fail"
    assert len(by_rule["sample_ids"].diagnostics) == 2
    assert by_rule["readme"].status == "warn"
    assert by_rule["e2e_test"].status == "suppressed"
    assert by_rule["sandbox_image_pinning"].status == "skip"
    # Execution order: file_structure (readme) before tests before best_practices.
    assert [rs.rule.name for rs in report.rule_statuses()] == [
        "readme",
        "e2e_test",
        "sample_ids",
        "sandbox_image_pinning",
    ]


def test_score_counts_rules_not_findings() -> None:
    report = report_with(
        ("sample_ids", "fail", None),
        ("sample_ids", "fail", None),
        ("sample_ids", "suppressed", None),
        ("readme", "warn", "TODO"),
        ("e2e_test", "suppressed", None),
        ("sandbox_image_pinning", "skip", None),
        ("main_file", "pass", None),
    )
    score = report.score()
    assert (score.pass_, score.fail, score.warn, score.suppressed, score.skip) == (1, 1, 1, 1, 1)
    assert score.applicable == 4  # skip is not applicable
    assert score.passing == 2  # pass + warn
    assert score.score == 0.5
    assert score.by_category["best_practices"].applicable == 1  # sample_ids; the skip is excluded
    assert score.by_category["tests"].to_dict(by_category=False) == {
        "pass": 0,
        "fail": 0,
        "warn": 0,
        "skip": 0,
        "suppressed": 1,
        "applicable": 1,
        "passing": 0,
        "score": 0.0,
    }
    assert report.summary()["fail"] == 2  # summary still counts findings


def test_suppressed_outranks_pass_so_views_agree() -> None:
    report = report_with(("sample_ids", "pass", None), ("sample_ids", "suppressed", None))
    assert report.rule_statuses()[0].status == "suppressed"
    assert report.score().suppressed == 1
    assert report.score().passing == 0


def test_empty_report_scores_none() -> None:
    score = PackageReport(name="x", kind="eval").score()
    assert score.applicable == 0
    assert score.score is None
    assert score.to_dict()["by_category"] == {}


def test_run_score_aggregates_packages_and_json_carries_scores(tmp_path: Path) -> None:
    a = report_with(("readme", "pass", None), ("e2e_test", "fail", None))
    b = report_with(("readme", "pass", None))
    b.name = "beta"
    run = RunReport(root=tmp_path, packages=[a, b])
    assert run.score().to_dict()["passing"] == 2
    assert run.score().to_dict()["applicable"] == 3
    doc = run.to_dict()
    assert doc["score"]["by_category"]["file_structure"]["passing"] == 2
    assert doc["packages"][0]["score"]["applicable"] == 2
    assert "by_category" not in doc["score"]["by_category"]["tests"]
    assert "score" in render_json(run)


def test_markdown_headline_lists_categories_and_folds_actionable_rules(tmp_path: Path) -> None:
    report = report_with(
        ("readme", "pass", None),
        ("e2e_test", "fail", "No E2E test found"),
        ("sample_ids", "warn", "Sample() call without id="),
        ("sample_ids", "suppressed", "Sample() call without id="),
        ("sandbox_image_pinning", "skip", None),
    )
    text = "\n".join(package_markdown(report, Path("/repo")))
    assert text.startswith("### `alpha`\n")
    assert (
        "**2/3 checks met** · Structure 1/1 · Code quality — · Tests 0/1 · Best practices 1/1"
        in text
    )
    assert "<summary>2 rule(s) not met, with warnings or suppressed</summary>" in text
    assert (
        "- **Not met** [`e2e_test`](https://inspect-evals-lint.generality.org/rules/IETS003/)"
        in text
    )
    assert "  - `src/alpha/alpha.py:7` No E2E test found<br>  Hint: do the thing" in text
    assert "- **Warning** [`sample_ids`]" in text
    assert "  - *suppressed* `src/alpha/alpha.py:7` Sample() call without id=" in text
    assert "readme" not in text.split("<details>", 1)[1]


def test_markdown_links_locations_when_asked(tmp_path: Path) -> None:
    report = report_with(("e2e_test", "fail", None))
    text = "\n".join(
        package_markdown(
            report,
            Path("/repo"),
            source_link=lambda path, line: f"https://x.test/blob/abc/{path}#L{line}",
            docs_base="https://docs.test/rules",
            heading_level=2,
            heading="Alpha at abc",
        )
    )
    assert text.startswith("## Alpha at abc\n")
    assert "[`src/alpha/alpha.py:7`](https://x.test/blob/abc/src/alpha/alpha.py#L7)" in text
    assert "(https://docs.test/rules/IETS003/)" in text


def test_markdown_clean_and_skipped_packages() -> None:
    clean = report_with(("readme", "pass", None), ("e2e_test", "skip", None))
    assert "Every applicable check is met." in "\n".join(package_markdown(clean))
    skipped = PackageReport(
        name="examples", kind="eval", skipped="'examples' is listed in ignore-dirs"
    )
    assert "Not linted: 'examples' is listed in ignore-dirs" in "\n".join(package_markdown(skipped))
    helper = PackageReport(name="utils", kind="helper")
    assert "\n".join(package_markdown(helper)).startswith("### `utils` (helper package)")


def test_markdown_escapes_text_from_the_linted_repository() -> None:
    assert code("a`b|c\nd") == "`a'b\\|c d`"
    assert plain("x  |  y\n z") == "x \\| y z"
    report = report_with(("e2e_test", "fail", "bad | `tick`\n<img src=x>"))
    text = "\n".join(package_markdown(report))
    assert "bad \\| `tick` <img src=x>" in text  # one line; pipes escaped; no fence opened


def test_render_markdown_ends_with_the_counting_rule(tmp_path: Path) -> None:
    run = RunReport(root=tmp_path, packages=[report_with(("readme", "pass", None))])
    text = render_markdown(run)
    assert text.endswith("Rule names link to their documentation.\n")
    assert render_markdown(run, footer=False).endswith("Every applicable check is met.\n")


def test_markdown_on_a_real_package(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "extra.py",
        "from inspect_ai.dataset import Sample\nSample(input='x')\n",
    )
    report = lint_package(root, "alpha", config)
    text = "\n".join(package_markdown(report, root))
    assert "[`sample_ids`]" in text
    assert "`src/inspect_evals/alpha/extra.py:2`" in text
