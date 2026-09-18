"""Suppression loading at package, directory, file and line level."""

from __future__ import annotations

from pathlib import Path

from inspect_evals_lint import LintConfig, lint_evaluation
from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
from tests.conftest import write


def test_package_level(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.eval_dir(root, "alpha")
    (eval_dir / "README.md").unlink()
    write(eval_dir / ".noautolint", "# comment\nreadme\n")
    report = lint_evaluation(root, "alpha", config)
    assert report.statuses()["readme"] == ["suppressed"]
    assert report.passed()


def test_line_level_by_name_or_code(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.eval_dir(root, "alpha")
    write(
        eval_dir / "private.py",
        "from inspect_ai.model._model import thing  # noautolint: private_api_imports\n"
        "from inspect_ai.solver._solver import other  # noautolint: IECQ001\n"
        "from inspect_ai.tool._tool import third\n",
    )
    report = lint_evaluation(root, "alpha", config, check="private_api_imports")
    assert sorted(report.statuses()["private_api_imports"]) == ["fail", "suppressed", "suppressed"]


def test_file_level_must_be_in_header(tmp_path: Path) -> None:
    eval_dir = tmp_path / "e"
    write(eval_dir / "late.py", "\n" * 12 + "# noautolint-file: readme\n")
    write(eval_dir / "early.py", "# noautolint-file: readme, sample_ids\n")
    s = load_suppressions(eval_dir)
    assert eval_dir / "late.py" not in s.file_level
    assert s.file_level[eval_dir / "early.py"] == {"readme", "sample_ids"}


def test_dir_level_applies_to_files_beneath(tmp_path: Path) -> None:
    eval_dir = tmp_path / "e"
    write(eval_dir / "sub" / ".noautolint", "sample_ids\n")
    write(eval_dir / "sub" / "deep" / "x.py", "")
    s = load_suppressions(eval_dir)
    assert s.covers({"sample_ids"}, eval_dir / "sub" / "deep" / "x.py", None)
    assert not s.covers({"sample_ids"}, eval_dir / "x.py", None)
    assert not s.covers({"readme"}, eval_dir / "sub" / "x.py", None)


def test_apply_marks_only_covered_diagnostics(tmp_path: Path) -> None:
    eval_dir = tmp_path / "e"
    write(eval_dir / ".noautolint", "readme\n")
    readme = get_rule("readme")
    registry = get_rule("registry")
    diagnostics = [
        Diagnostic("m", file=eval_dir / "README.md", rule=readme),
        Diagnostic("m", file=eval_dir / "x", severity="warning", rule=registry),
    ]
    apply_suppressions(diagnostics, load_suppressions(eval_dir))
    assert [d.status for d in diagnostics] == ["suppressed", "warn"]
