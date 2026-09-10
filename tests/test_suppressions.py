"""Suppression loading at eval, directory, file and line level."""

from __future__ import annotations

from pathlib import Path

from inspect_evals_lint import LintConfig, lint_evaluation
from inspect_evals_lint.models import LintResult
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
from tests.conftest import write


def test_eval_level(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.eval_dir(root, "alpha")
    (eval_dir / "README.md").unlink()
    write(eval_dir / ".noautolint", "# comment\nreadme\n")
    report = lint_evaluation(root, "alpha", config)
    readme = next(r for r in report.results if r.name == "readme")
    assert readme.status == "suppressed"
    assert readme.message.startswith("[suppressed]")
    assert report.passed()


def test_line_level(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.eval_dir(root, "alpha")
    write(
        eval_dir / "private.py",
        "from inspect_ai.model._model import thing  # noautolint: private_api_imports\n"
        "from inspect_ai.solver._solver import other\n",
    )
    report = lint_evaluation(root, "alpha", config, check="private_api_imports")
    assert sorted(r.status for r in report.results) == ["fail", "suppressed"]


def test_file_level_must_be_in_header(tmp_path: Path) -> None:
    eval_dir = tmp_path / "e"
    write(eval_dir / "late.py", "\n" * 12 + "# noautolint-file: readme\n")
    write(eval_dir / "early.py", "# noautolint-file: readme, sample_ids\n")
    s = load_suppressions(eval_dir)
    assert str(eval_dir / "late.py") not in s.file_level
    assert s.file_level[str(eval_dir / "early.py")] == {"readme", "sample_ids"}


def test_dir_level_applies_to_files_beneath(tmp_path: Path) -> None:
    eval_dir = tmp_path / "e"
    write(eval_dir / "sub" / ".noautolint", "sample_ids\n")
    write(eval_dir / "sub" / "deep" / "x.py", "")
    s = load_suppressions(eval_dir)
    assert s.is_suppressed("sample_ids", str(eval_dir / "sub" / "deep" / "x.py"))
    assert not s.is_suppressed("sample_ids", str(eval_dir / "x.py"))
    assert not s.is_suppressed("readme", str(eval_dir / "sub" / "x.py"))


def test_apply_only_touches_fail_and_warn(tmp_path: Path) -> None:
    eval_dir = tmp_path / "e"
    write(eval_dir / ".noautolint", "a\nb\nc\n")
    s = load_suppressions(eval_dir)
    results = [
        LintResult(name="a", status="fail", message="m"),
        LintResult(name="b", status="warn", message="m"),
        LintResult(name="c", status="pass", message="m"),
    ]
    apply_suppressions(results, s)
    assert [r.status for r in results] == ["suppressed", "suppressed", "pass"]
