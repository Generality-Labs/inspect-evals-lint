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


def test_list_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--list-rules") == 0
    out = capsys.readouterr().out
    assert "IEFS001" in out
    assert "package_location" in out
    assert "sandbox_image_pinning" in out


def test_list_rules_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--list-rules", "--output-format", "json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["code"] == "IEFS001"
    assert {"code", "name", "category", "scopes", "summary", "allowlist"} <= set(data[0])


def test_explain_by_code_and_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--explain", "IEBP002") == 0
    out = capsys.readouterr().out
    assert "model_role_resolution" in out
    assert "allowlists.model_role_resolution" in out
    assert "Why is this bad?" in out
    assert run("--explain", "readme", "--output-format", "json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["code"] == "IEFS006"
    assert "TODO" in data["doc"]
    from inspect_evals_lint.docs import GENERATED_NOTE

    page = Path("docs/rules/IEFS006.md").read_text(encoding="utf-8")
    assert data["doc"] == page.replace(GENERATED_NOTE + "\n\n", "")


def test_explain_unknown_rule_is_usage_error() -> None:
    assert run("--explain", "bogus") == 2


def test_unknown_selector_is_usage_error(tmp_path: Path) -> None:
    make_template_repo(tmp_path)
    assert run("--all", "--root", str(tmp_path), "--select", "NOPE") == 2
    assert run("--all", "--root", str(tmp_path), "--ignore", "IEXX") == 2


def test_requires_package_or_all() -> None:
    assert run() == 2


def test_single_package_pass(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = monorepo
    assert run("alpha", "--root", str(root)) == 0
    assert "All required checks passed" in capsys.readouterr().out


def test_single_package_fail_prints_hints(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = monorepo
    (config.package_dir(root, "alpha") / "README.md").unlink()
    assert run("alpha", "--root", str(root)) == 1
    out = capsys.readouterr().out
    assert "IEFS006 readme" in out
    assert "# inspect-evals-lint: ignore[readme]" in out
    assert 'per-file-ignores = { "src/inspect_evals/alpha/**"' in out


def test_all_prints_summaries(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_template_repo(tmp_path, eval_names=("alpha", "beta"))
    assert run("--all", "--root", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Linting 2 evaluations and 1 helper package" in out
    assert "3/3 packages passed" in out
    assert "utils (helper)" in out
    assert "CHECK COMPLIANCE SUMMARY" in out
    assert "using the 'template' preset" in out


def test_several_named_packages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_template_repo(tmp_path, eval_names=("alpha", "beta"))
    assert run("alpha", "beta", "--root", str(tmp_path), "--output-format", "json") == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in data["packages"]] == ["alpha", "beta"]


def test_select_and_ignore_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_template_repo(tmp_path)
    assert (
        run(
            "alpha",
            "--root",
            str(tmp_path),
            "--select",
            "IEFS",
            "--ignore",
            "readme",
            "--output-format",
            "json",
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    (package,) = data["packages"]
    ran = {o["rule"] for o in package["outcomes"]} | {d["rule"] for d in package["diagnostics"]}
    assert ran == {"package_location", "main_file", "init_exports", "registry", "eval_yaml"}


def test_preset_flag_overrides_table(tmp_path: Path) -> None:
    make_template_repo(tmp_path)
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'monorepo'\n")
    assert (
        run("alpha", "--root", str(tmp_path)) == 1
    )  # monorepo layout: src/inspect_evals/alpha missing
    assert run("alpha", "--root", str(tmp_path), "--preset", "template") == 1  # no entry points now
    assert run("alpha", "--root", str(tmp_path), "--preset", "template", "--select", "readme") == 0


def test_config_error_is_exit_2(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'nope'\n")
    assert run("--all", "--root", str(tmp_path)) == 2


def test_legacy_suppression_is_a_warning_not_exit_2(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = monorepo
    write(config.package_dir(root, "alpha") / ".noautolint", "readme\n")
    assert run("alpha", "--root", str(root)) == 0
    assert "suppression_syntax" in capsys.readouterr().out


def test_json_single_package(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = monorepo
    (config.package_dir(root, "alpha") / "README.md").unlink()
    assert run("alpha", "--root", str(root), "--output-format", "json") == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # stdout is the document and nothing else
    assert data["passed"] is False
    assert data["root"] == str(root.resolve())
    (package,) = data["packages"]
    assert package["name"] == "alpha"
    (readme,) = [d for d in package["diagnostics"] if d["rule"] == "readme"]
    assert readme["status"] == "fail"
    assert readme["code"] == "IEFS006"
    assert readme["file"] == "src/inspect_evals/alpha/README.md"


def test_json_all_sends_progress_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_template_repo(tmp_path, eval_names=("alpha", "beta"))
    assert run("--all", "--root", str(tmp_path), "--output-format", "json") == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["passed"] is True
    assert [(p["name"], p["kind"]) for p in data["packages"]] == [
        ("alpha", "eval"),
        ("beta", "eval"),
        ("utils", "helper"),
    ]
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
    assert run("--all", "--root", str(root)) == 1
    assert 'per-file-ignores = { "src/inspect_evals/utils/**"' in capsys.readouterr().out


def test_missing_table_message_keeps_brackets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_template_repo(tmp_path)
    assert run("--all", "--root", str(tmp_path)) == 0
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
    main_file = config.package_dir(root, "alpha") / "alpha.py"
    main_file.write_text(
        '"""Évaluation — naïve façade."""\n' + main_file.read_text(), encoding="utf-8"
    )
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    for var in ("PYTHONIOENCODING", "LANG", "LC_CTYPE"):
        env.pop(var, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "inspect_evals_lint",
            "alpha",
            "--root",
            str(root),
            "--output-format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True


def test_task_flag_lints_the_package_holding_the_file(
    register_repo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = register_repo
    assert (
        run(
            "--task",
            "src/alpha/alpha.py",
            "--root",
            str(root),
            "--preset",
            "register",
            "--output-format",
            "json",
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in data["packages"]] == ["alpha"]
    assert data["score"]["applicable"] > 0


def test_task_flag_rejects_a_bare_module_as_usage_error(
    register_repo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = register_repo
    write(root / "task.py", "")
    assert run("--task", "task.py", "--root", str(root)) == 2
    assert "not inside a package" in capsys.readouterr().err


def test_task_flag_cannot_be_combined_with_packages(register_repo: tuple[Path, LintConfig]) -> None:
    root, _ = register_repo
    assert run("--task", "src/alpha/alpha.py", "alpha", "--root", str(root)) == 2


def test_markdown_output(
    monorepo: tuple[Path, LintConfig], capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = monorepo
    (config.package_dir(root, "alpha") / "README.md").unlink()
    assert run("alpha", "--root", str(root), "--output-format", "markdown") == 1
    out = capsys.readouterr().out
    assert out.startswith("### `alpha`\n")
    assert "- **Not met** [`readme`]" in out
    assert "checks met" in out


def test_github_format_appends_the_markdown_summary_to_the_job_summary(
    monorepo: tuple[Path, LintConfig],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GITHUB_STEP_SUMMARY is where a run is readable in full; the log shows messages only."""
    root, config = monorepo
    (config.package_dir(root, "alpha") / "README.md").unlink()
    summary = tmp_path / "step_summary.md"
    summary.write_text("# earlier step\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert run("alpha", "--root", str(root), "--output-format", "github") == 1
    out = capsys.readouterr().out
    assert "::error file=src/inspect_evals/alpha/README.md,title=IEFS006 readme::" in out
    assert "src/inspect_evals/alpha/README.md IEFS006 readme: " in out
    written = summary.read_text(encoding="utf-8")
    assert written.startswith(
        "# earlier step\n## inspect-evals-lint\n\n### `alpha`"
    )  # appended, not replaced
    assert "- **Not met** [`readme`]" in written
    assert "`src/inspect_evals/alpha/README.md`" in written


def test_github_format_without_a_job_summary_writes_nothing_extra(
    monorepo: tuple[Path, LintConfig], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = monorepo
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert run("alpha", "--root", str(root), "--output-format", "github") == 0
    assert list(tmp_path.iterdir()) == [
        p for p in tmp_path.iterdir() if p.name != "step_summary.md"
    ]
