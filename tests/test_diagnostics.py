"""Per-site diagnostics: every finding points at a file and, where it applies, a line."""

from __future__ import annotations

from pathlib import Path

from inspect_evals_lint import LintConfig, lint_package
from inspect_evals_lint.diagnostics import Diagnostic, Outcome, PackageReport, RunReport
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.rules.best_practices import sample_ids, task_overridable_defaults
from inspect_evals_lint.rules.code_quality import score_constants
from tests.conftest import context_for, write


def _diagnostics(report: PackageReport, name: str) -> list[Diagnostic]:
    return [d for d in report.diagnostics if d.rule is not None and d.rule.name == name]


def test_every_finding_has_a_file(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.package_dir(root, "alpha")
    (eval_dir / "README.md").unlink()
    (eval_dir / "eval.yaml").write_text("title: Alpha\n")
    write(eval_dir / "extra.py", "import definitely_not_installed_pkg\nx = Sample(input='q')\n")
    report = lint_package(root, "alpha", config)
    assert report.diagnostics, "expected findings"
    for d in report.diagnostics:
        assert isinstance(d.file, Path), d
        assert d.rule is not None, d


def test_sample_ids_reports_each_call(tmp_path: Path) -> None:
    pkg = tmp_path / "alpha"
    write(pkg / "__init__.py", "")
    write(
        pkg / "data.py",
        "a = Sample(input='x', id='1')\nb = Sample(input='y')\nc = [\n    Sample(input='z'),\n]\n",
    )
    findings = list(sample_ids(context_for(pkg)))
    assert [(type(f).__name__, getattr(f, "line", None)) for f in findings] == [
        ("Diagnostic", 2),
        ("Diagnostic", 4),
    ]
    assert findings[0].column == 5  # type: ignore[union-attr]


def test_sample_ids_passes_and_skips(tmp_path: Path) -> None:
    pkg = tmp_path / "alpha"
    write(pkg / "__init__.py", "")
    write(pkg / "data.py", "a = Sample(input='x', id='1')\n")
    (outcome,) = sample_ids(context_for(pkg))
    assert isinstance(outcome, Outcome)
    assert outcome.status == "pass"
    write(pkg / "data.py", "a = 1\n")
    (outcome,) = sample_ids(context_for(pkg))
    assert isinstance(outcome, Outcome)
    assert outcome.status == "skip"


def test_task_defaults_report_each_parameter(tmp_path: Path) -> None:
    pkg = tmp_path / "alpha"
    write(pkg / "__init__.py", "")
    write(
        pkg / "alpha.py",
        "from inspect_ai import task\n\n@task\ndef alpha(solver, scorer=None, grader_model=None, *, metrics):\n    ...\n",
    )
    findings = list(task_overridable_defaults(context_for(pkg)))
    messages = [f.message for f in findings]
    assert messages == [
        "@task alpha() parameter 'solver' has no default",
        "@task alpha() parameter 'metrics' has no default",
    ]
    assert {f.line for f in findings} == {4}  # type: ignore[union-attr]


def test_score_constants_report_each_literal(tmp_path: Path) -> None:
    pkg = tmp_path / "alpha"
    write(pkg / "__init__.py", "")
    write(
        pkg / "s.py",
        "a = Score(value='C')\nb = Score(value=CORRECT)\nc = Score(value='INCORRECT')\n",
    )
    findings = list(score_constants(context_for(pkg)))
    assert [(f.status, f.line) for f in findings] == [("fail", 1), ("fail", 3)]  # type: ignore[union-attr]
    assert "CORRECT" in (findings[0].hint or "")  # type: ignore[union-attr]


def test_readme_todos_report_each_line(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(config.package_dir(root, "alpha") / "README.md", "# alpha\n\nTODO: a\n\nfine\nTODO: b\n")
    report = lint_package(root, "alpha", config, check="readme")
    assert [(d.status, d.line) for d in report.diagnostics] == [("warn", 3), ("warn", 6)]
    assert report.passed()


def test_eval_yaml_reports_each_missing_field(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    (config.package_dir(root, "alpha") / "eval.yaml").write_text("title: Alpha\n")
    report = lint_package(root, "alpha", config, check="eval_yaml")
    fields = [d.message.split("'")[1] for d in report.diagnostics]
    assert fields == ["description", "group", "contributors", "tasks"]
    assert all(d.file.name == "eval.yaml" for d in report.diagnostics)


def test_init_exports_reports_each_missing_task(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.package_dir(root, "alpha")
    write(
        eval_dir / "alpha.py",
        "from inspect_ai import task\n\n@task\ndef alpha(): ...\n\n@task\ndef beta(): ...\n",
    )
    write(eval_dir / "__init__.py", "")
    report = lint_package(root, "alpha", config, check="init_exports")
    assert [d.message for d in report.diagnostics] == [
        "__init__.py does not export the @task function 'alpha'",
        "__init__.py does not export the @task function 'beta'",
    ]
    assert "from .alpha import alpha" in (report.diagnostics[0].hint or "")


def test_tests_init_reports_each_directory(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(root / "tests/alpha/fixtures/a.txt", "")
    write(root / "tests/alpha/more/deep/x.py", "")
    report = lint_package(root, "alpha", config, check="tests_init")
    dirs = sorted(d.file.relative_to(root).as_posix() for d in report.diagnostics)
    assert dirs == ["tests/alpha/fixtures", "tests/alpha/more", "tests/alpha/more/deep"]


def test_custom_component_diagnostics_point_at_the_definition(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "scorer.py",
        "from inspect_ai.scorer import scorer\n\n\n@scorer(metrics=[])\ndef my_scorer():\n    ...\n",
    )
    report = lint_package(root, "alpha", config, check="custom_scorer_tests")
    (d,) = report.diagnostics
    assert d.file.name == "scorer.py"
    assert d.line == 5
    assert "my_scorer" in d.message


def test_external_dependency_diagnostics_point_at_the_import(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "extra.py",
        "import os\n\nimport definitely_not_installed_pkg\n",
    )
    report = lint_package(root, "alpha", config, check="external_dependencies")
    (d,) = report.diagnostics
    assert (d.file.name, d.line, d.column) == ("extra.py", 3, 1)
    assert "definitely_not_installed_pkg" in d.message


def test_rule_yielding_nothing_passes_with_its_summary(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    report = lint_package(root, "alpha", config, check="readme")
    (outcome,) = report.outcomes
    assert outcome.status == "pass"


def test_items_are_grouped_by_rule_with_the_outcome_first() -> None:
    readme = get_rule("readme")
    registry = get_rule("registry")
    report = PackageReport("x", "eval")
    report.add(Diagnostic("d1", file=Path("a"), rule=readme))
    report.add(Outcome("pass", "ok", rule=registry))
    report.add(Diagnostic("d2", file=Path("b"), severity="warning", rule=readme))
    report.add(Outcome("skip", "meh", rule=readme))
    # registry is IEFS004 and readme IEFS006, so registry's group comes first.
    assert [(i.rule.name, i.status) for i in report.items()] == [  # type: ignore[union-attr]
        ("registry", "pass"),
        ("readme", "skip"),
        ("readme", "fail"),
        ("readme", "warn"),
    ]
    assert report.statuses() == {"registry": ["pass"], "readme": ["skip", "fail", "warn"]}
    assert report.rules_run() == ["registry", "readme"]


def test_run_report_totals_and_kinds() -> None:
    a = PackageReport("a", "eval")
    a.add(Outcome("pass", "ok", rule=get_rule("readme")))
    b = PackageReport("utils", "helper")
    b.add(Diagnostic("bad", file=Path("x"), rule=get_rule("readme")))
    run = RunReport(root=Path("/r"), packages=[a, b])
    assert run.passed() is False
    assert run.summary()["pass"] == 1
    assert run.summary()["fail"] == 1
    assert [p.name for p in run.of_kind("helper")] == ["utils"]
