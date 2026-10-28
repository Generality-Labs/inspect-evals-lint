"""ruff_rules_enabled: the ruff configuration governing a package enables the curated ruff rules.

These tests run the real ruff, which the dev dependency group installs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint.config import PRESETS, ConfigError, LintConfig, config_from_table
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.rules import ruff_config
from inspect_evals_lint.rules.ruff_config import (
    CURATED_RULES,
    CuratedRule,
    RuffError,
    find_ruff,
    governing_config,
    ruff_rules_enabled,
)
from inspect_evals_lint.runner import lint_repository, lint_task_files
from tests.conftest import context_for, make_monorepo, make_register_repo, write

PYPROJECT = '[project]\nname = "my-eval"\n\n'
PLW_WITHOUT_PREVIEW = '[tool.ruff.lint]\nselect = ["E", "F", "PLW"]\n'
PLW_WITH_PREVIEW = '[tool.ruff.lint]\nselect = ["E", "F", "PLW"]\npreview = true\n'


def _run(root: Path, config: LintConfig | None = None) -> list[Finding]:
    """Lint ``root/src/alpha``, a package with one PLW1514 violation, as a single-eval repository."""
    write(root / "src/alpha/__init__.py", "")
    write(root / "src/alpha/data.py", 'TEXT = open("x.txt").read()\n')
    ctx = replace(context_for(root / "src/alpha", config or PRESETS["single-eval"]), root=root)
    return list(ruff_rules_enabled(ctx))


def _statuses(results: list[Finding]) -> list[str]:
    return [r.status for r in results]


@pytest.mark.parametrize("entry", CURATED_RULES, ids=lambda e: e.code)
def test_each_probe_is_reported_by_its_rule(tmp_path: Path, entry: CuratedRule) -> None:
    ruff = find_ruff()
    assert ruff is not None
    found = ruff_config._ruff_json(  # pyright: ignore[reportPrivateUsage]
        ruff,
        [
            *("check", "--isolated", "--no-cache", "--exit-zero", "--output-format", "json"),
            *("--select", entry.code, "--preview", "--stdin-filename", "probe.py", "-"),
        ],
        cwd=tmp_path,
        stdin=entry.probe,
    )
    assert [d["code"] for d in found] == [entry.code]


def test_the_rule_page_lists_every_curated_rule() -> None:
    found = get_rule("IECQ007")
    assert found is not None
    for entry in CURATED_RULES:
        assert entry.code in found.doc
        assert entry.name in found.doc
    assert "{curated_rules}" not in found.doc


def test_a_preview_rule_selected_by_prefix_without_preview_is_not_enabled(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", PYPROJECT + PLW_WITHOUT_PREVIEW)
    [result] = _run(tmp_path)
    assert isinstance(result, Diagnostic)
    assert result.status == "warn"
    assert result.file == tmp_path / "pyproject.toml"
    assert result.message == (
        "pyproject.toml does not enable ruff rule PLW1514 (unspecified-encoding); "
        "enabling it reports 1 violation(s) in 1 file(s) under src"
    )
    assert result.hint is not None
    assert 'add "PLW1514" to extend-select and set preview = true' in result.hint
    assert '"PLW"' in result.hint


def test_preview_with_the_prefix_selected_passes(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", PYPROJECT + PLW_WITH_PREVIEW)
    assert _run(tmp_path) == []


def test_explicit_preview_rules_need_the_code_named(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        PYPROJECT + PLW_WITH_PREVIEW + "explicit-preview-rules = true\n",
    )
    assert _statuses(_run(tmp_path)) == ["warn"]
    write(
        tmp_path / "pyproject.toml",
        PYPROJECT
        + PLW_WITH_PREVIEW
        + 'explicit-preview-rules = true\nextend-select = ["PLW1514"]\n',
    )
    assert _run(tmp_path) == []


def test_an_ignored_rule_is_not_enabled(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", PYPROJECT + PLW_WITH_PREVIEW + 'ignore = ["PLW1514"]\n')
    assert _statuses(_run(tmp_path)) == ["warn"]


def test_per_file_ignores_covering_the_package_disable_it_there(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        PYPROJECT
        + PLW_WITH_PREVIEW
        + '[tool.ruff.lint.per-file-ignores]\n"src/alpha/**" = ["PLW1514"]\n',
    )
    [result] = _run(tmp_path)
    assert result.status == "warn"
    assert isinstance(result, Diagnostic)
    # The ignore applies to the count too, so it reports none.
    assert "enabling it reports no violations under src" in result.message


def test_a_ruff_toml_is_the_configuration(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", PYPROJECT)
    write(tmp_path / "ruff.toml", '[lint]\nextend-select = ["PLW1514"]\npreview = true\n')
    assert _run(tmp_path) == []
    write(tmp_path / "ruff.toml", '[lint]\nselect = ["PLW"]\n')
    [result] = _run(tmp_path)
    assert result.status == "warn"
    assert isinstance(result, Diagnostic)
    assert result.file == tmp_path / "ruff.toml"
    assert result.message.startswith("ruff.toml does not enable")


def test_a_rule_enabled_through_extend_is_enabled(tmp_path: Path) -> None:
    write(tmp_path / "configs/base.toml", '[lint]\nextend-select = ["PLW1514"]\npreview = true\n')
    write(tmp_path / "pyproject.toml", PYPROJECT + '[tool.ruff]\nextend = "configs/base.toml"\n')
    assert _run(tmp_path) == []


def test_without_any_ruff_configuration_the_defaults_do_not_enable_it(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", PYPROJECT)
    [result] = _run(tmp_path)
    assert isinstance(result, Diagnostic)
    assert result.status == "warn"
    assert result.file == tmp_path / "pyproject.toml"
    assert result.message.startswith(
        "No ruff configuration applies, and ruff's defaults do not enable ruff rule PLW1514"
    )
    assert "1 violation(s) in 1 file(s) under src" in result.message


def test_a_configuration_outside_the_repository_is_not_read(tmp_path: Path) -> None:
    write(tmp_path / "ruff.toml", '[lint]\nextend-select = ["PLW1514"]\npreview = true\n')
    root = tmp_path / "repo"
    write(root / "pyproject.toml", PYPROJECT)
    assert governing_config(root, root / "src/alpha") is None
    [result] = _run(root)
    assert result.status == "warn"


def test_governing_config_follows_ruffs_precedence(tmp_path: Path) -> None:
    package = tmp_path / "src/alpha"
    package.mkdir(parents=True)
    write(tmp_path / "pyproject.toml", PYPROJECT)
    assert governing_config(tmp_path, package) is None
    write(tmp_path / "pyproject.toml", PYPROJECT + PLW_WITH_PREVIEW)
    assert governing_config(tmp_path, package) == tmp_path / "pyproject.toml"
    write(tmp_path / "ruff.toml", "")
    assert governing_config(tmp_path, package) == tmp_path / "ruff.toml"
    write(tmp_path / ".ruff.toml", "")
    assert governing_config(tmp_path, package) == tmp_path / ".ruff.toml"
    write(tmp_path / "src/ruff.toml", "")
    assert governing_config(tmp_path, package) == tmp_path / "src/ruff.toml"


def test_a_declined_rule_is_suppressed_with_its_reason(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", PYPROJECT + PLW_WITHOUT_PREVIEW)
    config = config_from_table(
        {
            "preset": "single-eval",
            "ruff_rules_enabled": {"declined": {"PLW1514": "every data file is ASCII"}},
        }
    )
    [result] = _run(tmp_path, config)
    assert isinstance(result, Diagnostic)
    assert result.status == "suppressed"
    assert result.message.endswith("; declined: every data file is ASCII")


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"declined": {"PLW9999": "x"}}, "not a curated rule"),
        ({"declined": {"PLW1514": " "}}, "must give a reason"),
        ({"declined": ["PLW1514"]}, "must be a table"),
        ({"skip": True}, "unknown option"),
    ],
)
def test_bad_options_are_configuration_errors(
    tmp_path: Path, options: dict[str, object], match: str
) -> None:
    config = config_from_table({"preset": "single-eval", "ruff_rules_enabled": options})
    with pytest.raises(ConfigError, match=match):
        _run(tmp_path, config)


def test_skips_without_ruff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ruff_config, "find_ruff", lambda: None)
    [result] = _run(tmp_path)
    assert isinstance(result, Outcome)
    assert result.status == "skip"
    assert "ruff is not installed" in result.message


def test_skips_when_ruff_cannot_read_the_configuration(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", PYPROJECT + '[tool.ruff.lint]\nselect = ["NOPE1"]\n')
    [result] = _run(tmp_path)
    assert isinstance(result, Outcome)
    assert result.status == "skip"
    assert "NOPE1" in result.message


@pytest.mark.parametrize(
    "pyproject",
    [
        PYPROJECT + '[tool.ruff.lint\nselect = ["PLW"]\n',
        "tool = 1\n",
        'tool = "ruffian"\n',
    ],
    ids=["invalid-toml", "tool-integer", "tool-string"],
)
def test_skips_a_pyproject_ruff_cannot_read(tmp_path: Path, pyproject: str) -> None:
    write(tmp_path / "pyproject.toml", pyproject)
    [result] = _run(tmp_path)
    assert isinstance(result, Outcome)
    assert result.status == "skip"
    assert result.message.startswith("ruff could not resolve its configuration: Failed to parse")


def _fake_ruff(tmp_path: Path, failing: str) -> tuple[Path, Path]:
    """A ruff that knows PLW1514, logs each call, and fails every ``check`` whose arguments contain ``failing``."""
    log = tmp_path / "calls.log"
    script = write(
        tmp_path / "bin/ruff",
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'if [ "$1" = "rule" ]; then echo \'{"preview": true}\'; exit 0; fi\n'
        'if [ "$1" = "--version" ]; then echo "ruff 0.0.0"; exit 0; fi\n'
        f'case " $* " in *" {failing} "*) echo "Cause: boom" >&2; exit 2;; esac\n'
        "echo '[]'\n",
    )
    script.chmod(0o755)
    return script, log


@pytest.mark.parametrize(
    ("failing", "expected"),
    [("-", "ruff could not resolve its configuration: boom"), ("--select", None)],
    ids=["configuration", "count"],
)
def test_a_failing_ruff_is_tried_once_per_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str, expected: str | None
) -> None:
    script, log = _fake_ruff(tmp_path / "fake", failing)
    monkeypatch.setattr(ruff_config, "find_ruff", lambda: str(script))
    root = tmp_path / "repo"
    config = make_monorepo(root, ("alpha", "beta", "gamma"))
    run = lint_repository(root, config, check="ruff_rules_enabled")
    failed = [line for line in log.read_text().splitlines() if f" {failing} " in f" {line} "]
    assert len(failed) == 1
    if expected is not None:
        assert all([o.message for o in p.outcomes] == [expected] for p in run.packages)
    else:
        [finding] = run.packages[0].diagnostics
        assert "enabling it reports" not in finding.message


def test_skips_a_rule_this_ruff_does_not_know(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = CuratedRule("PLX9999", "no-such-rule", "", "", "x = 1\n")
    monkeypatch.setattr(ruff_config, "CURATED_RULES", (unknown,))
    [result] = _run(tmp_path)
    assert isinstance(result, Outcome)
    assert result.status == "skip"
    assert "does not know PLX9999" in result.message


def test_the_message_leaves_out_a_count_ruff_could_not_make(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> tuple[int, int]:
        raise RuffError("boom")

    monkeypatch.setattr(ruff_config, "_violations", fail)
    write(tmp_path / "pyproject.toml", PYPROJECT + PLW_WITHOUT_PREVIEW)
    [result] = _run(tmp_path)
    assert (
        result.message == "pyproject.toml does not enable ruff rule PLW1514 (unspecified-encoding)"
    )


def test_a_monorepo_reports_its_shared_configuration_once(tmp_path: Path) -> None:
    config = make_monorepo(tmp_path, ("alpha", "beta"))
    with (tmp_path / "pyproject.toml").open("a") as f:
        f.write(PLW_WITHOUT_PREVIEW)
    run = lint_repository(tmp_path, config, check="ruff_rules_enabled")
    by_package = {p.name: p for p in run.packages}
    assert [d.status for d in by_package["alpha"].diagnostics] == ["warn"]
    for name in ("beta", "utils"):
        assert by_package[name].diagnostics == []
        [outcome] = by_package[name].outcomes
        assert outcome.status == "skip"
        assert "Reported under 'alpha'" in outcome.message


def test_a_package_with_its_own_configuration_is_reported_separately(tmp_path: Path) -> None:
    config = make_monorepo(tmp_path, ("alpha", "beta"))
    with (tmp_path / "pyproject.toml").open("a") as f:
        f.write(PLW_WITHOUT_PREVIEW)
    write(tmp_path / "src/inspect_evals/beta/ruff.toml", '[lint]\nselect = ["PLW"]\n')
    run = lint_repository(tmp_path, config, names=["alpha", "beta"], check="ruff_rules_enabled")
    files = [d.file.relative_to(tmp_path).as_posix() for p in run.packages for d in p.diagnostics]
    assert files == ["pyproject.toml", "src/inspect_evals/beta/ruff.toml"]


def test_a_task_run_reports_the_finding_for_its_one_package(tmp_path: Path) -> None:
    make_register_repo(tmp_path)
    with (tmp_path / "pyproject.toml").open("a") as f:
        f.write(PLW_WITHOUT_PREVIEW)
    run = lint_task_files(tmp_path, ["src/alpha/alpha.py"], check="ruff_rules_enabled")
    [package] = run.packages
    [result] = package.diagnostics
    assert result.status == "warn"
    assert result.message.startswith("pyproject.toml does not enable ruff rule PLW1514")
