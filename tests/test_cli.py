"""CLI exit codes and modes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    assert "Linting 2 evaluations and 1 helper package" in out
    assert "3/3 packages passed" in out
    assert "utils (helper)" in out
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


def test_json_single_eval(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = monorepo
    (config.eval_dir(root, "alpha") / "README.md").unlink()
    assert run("alpha", "--root", str(root), "--json") == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # stdout is the document and nothing else
    assert data["passed"] is False
    assert data["root"] == str(root.resolve())
    (evaluation,) = data["evaluations"]
    assert evaluation["name"] == "alpha"
    readme = next(r for r in evaluation["results"] if r["check"] == "readme")
    assert readme["status"] == "fail"
    assert readme["file"] == "src/inspect_evals/alpha/README.md"


def test_json_all_evals_sends_progress_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_template_repo(tmp_path, eval_names=("alpha", "beta"))
    assert run("--all-evals", "--root", str(tmp_path), "--json") == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["passed"] is True
    assert data["evaluations_total"] == 2
    assert [e["name"] for e in data["evaluations"]] == ["alpha", "beta"]
    assert data["helpers_total"] == 1
    assert [(h["name"], h["kind"]) for h in data["helpers"]] == [("utils", "helper")]
    assert "Linting 2 evaluations and 1 helper package" in captured.err
    assert "No [tool.inspect-evals-lint] table" in captured.err


def test_helper_package_can_be_named_directly(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = monorepo
    assert run("utils", "--root", str(root)) == 0
    out = capsys.readouterr().out
    assert "Lint Report: utils (helper package)" in out
    assert "model_role_resolution" in out
    assert "readme" not in out


def test_helper_failure_sets_exit_code(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = monorepo
    write(
        config.source_dir(root) / "utils" / "grader.py",
        'from inspect_ai.model import get_model\n\ngrader = get_model(role="grader")\n',
    )
    assert run("--all-evals", "--root", str(root)) == 1
    assert "src/inspect_evals/utils/.noautolint" in capsys.readouterr().out


def test_json_with_check_summary_still_emits_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_template_repo(tmp_path)
    assert run("--check-summary", "--root", str(tmp_path), "--json", "--check", "readme") == 0
    data = json.loads(capsys.readouterr().out)
    assert {r["check"] for e in data["evaluations"] for r in e["results"]} == {"readme"}


def test_missing_table_message_keeps_brackets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_template_repo(tmp_path)
    assert run("--all-evals", "--root", str(tmp_path)) == 0
    assert "No [tool.inspect-evals-lint] table" in capsys.readouterr().out


@pytest.mark.skipif(
    sys.platform != "linux", reason="forces an ASCII locale the way only glibc does"
)
def test_non_utf8_locale_still_reads_source_files(monorepo: tuple[Path, LintConfig]) -> None:
    """Every file read passes encoding="utf-8", so a cp1252/ASCII default locale cannot break a run.

    Ported from UKGovernmentBEIS/inspect_evals#2322. PEP 538/540 normally rescue a C
    locale, so both are disabled to make Python's default encoding really be ASCII.
    """
    root, config = monorepo
    main_file = config.eval_dir(root, "alpha") / "alpha.py"
    main_file.write_text(
        '"""Évaluation — naïve façade."""\n' + main_file.read_text(), encoding="utf-8"
    )
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    for var in ("PYTHONIOENCODING", "LANG", "LC_CTYPE"):
        env.pop(var, None)
    result = subprocess.run(
        [sys.executable, "-m", "inspect_evals_lint", "alpha", "--root", str(root), "--json"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True
