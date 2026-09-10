"""CLI exit codes and modes."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspect_evals_lint import LintConfig
from inspect_evals_lint.cli import main
from tests.conftest import make_template_repo, write


def run(*argv: str) -> int:
    with pytest.raises(SystemExit) as exc:
        main(list(argv))
    return int(exc.value.code or 0)


def test_list_checks(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--list-checks") == 0
    out = capsys.readouterr().out
    assert "eval_location" in out
    assert "sandbox_image_pinning" in out


def test_unknown_check_is_usage_error() -> None:
    assert run("--check", "bogus", "--all-evals") == 2


def test_requires_eval_or_all() -> None:
    assert run() == 2


def test_single_eval_pass(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = monorepo
    assert run("alpha", "--root", str(root)) == 0
    assert "All required checks passed" in capsys.readouterr().out


def test_single_eval_fail_prints_hints(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = monorepo
    (config.eval_dir(root, "alpha") / "README.md").unlink()
    assert run("alpha", "--root", str(root)) == 1
    out = capsys.readouterr().out
    assert "# noautolint: readme" in out
    assert "src/inspect_evals/alpha/.noautolint" in out


def test_all_evals_and_check_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_template_repo(tmp_path, eval_names=("alpha", "beta"))
    assert run("--all-evals", "--root", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Linting 2 evaluations" in out
    assert "2/2 evaluations passed" in out
    assert "using the 'template' preset" in out

    assert run("--check-summary", "--root", str(tmp_path)) == 0
    assert "CHECK COMPLIANCE SUMMARY" in capsys.readouterr().out


def test_preset_flag_overrides_table(tmp_path: Path) -> None:
    make_template_repo(tmp_path)
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'monorepo'\n")
    assert (
        run("alpha", "--root", str(tmp_path)) == 1
    )  # monorepo layout: src/inspect_evals/alpha missing
    assert run("alpha", "--root", str(tmp_path), "--preset", "template") == 1  # no entry points now
    assert run("alpha", "--root", str(tmp_path), "--preset", "template", "--check", "readme") == 0


def test_config_error_is_exit_2(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'nope'\n")
    assert run("--all-evals", "--root", str(tmp_path)) == 2
