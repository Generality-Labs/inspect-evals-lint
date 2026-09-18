"""Ignore comments and path-based ignores."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspect_evals_lint import ConfigError, LintConfig, lint_evaluation
from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
from tests.conftest import write


def test_line_level_by_name_or_code(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.eval_dir(root, "alpha")
    write(
        eval_dir / "private.py",
        "from inspect_ai.model._model import a  # inspect-evals-lint: ignore[private_api_imports]\n"
        "from inspect_ai.solver._solver import b  # inspect-evals-lint: ignore[IECQ001]\n"
        "from inspect_ai.tool._tool import c  # inspect-evals-lint: ignore[IECQ]\n"
        "from inspect_ai.util._util import d  # inspect-evals-lint: ignore[readme, IEBP]\n"
        "from inspect_ai.log._log import e\n",
    )
    report = lint_evaluation(root, "alpha", config, check="private_api_imports")
    assert sorted(report.statuses()["private_api_imports"]) == [
        "fail",
        "fail",
        "suppressed",
        "suppressed",
        "suppressed",
    ]


def test_file_level_in_header(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "early.py", "# inspect-evals-lint: ignore-file[readme, sample_ids]\n")
    s = load_suppressions(pkg)
    assert s.file_level[pkg / "early.py"] == {"readme", "sample_ids"}


def test_file_level_outside_header_is_an_error(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "late.py", "\n" * 12 + "# inspect-evals-lint: ignore-file[readme]\n")
    with pytest.raises(ConfigError, match="first 10 lines"):
        load_suppressions(pkg)


@pytest.mark.parametrize(
    "comment",
    [
        "# inspect-evals-lint: ignore",
        "# inspect-evals-lint: ignore[]",
        "# inspect-evals-lint: ignore-file",
    ],
)
def test_ignore_without_a_rule_is_an_error(tmp_path: Path, comment: str) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", f"x = 1  {comment}\n")
    with pytest.raises(ConfigError, match="must name at least one rule"):
        load_suppressions(pkg)


@pytest.mark.parametrize(
    "setup",
    ["comment", "file-comment", "dotfile"],
)
def test_legacy_syntax_is_an_error_naming_the_replacement(tmp_path: Path, setup: str) -> None:
    pkg = tmp_path / "e"
    if setup == "comment":
        write(pkg / "x.py", "x = 1  # noautolint: readme\n")
    elif setup == "file-comment":
        write(pkg / "x.py", "# noautolint-file: readme\n")
    else:
        write(pkg / ".noautolint", "readme\n")
    with pytest.raises(ConfigError, match="ignore\\[<rule>\\]"):
        load_suppressions(pkg)


def test_apply_marks_covered_diagnostics(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "e"
    write(pkg / "a.py", "x = 1  # inspect-evals-lint: ignore[IEFS006]\n")
    readme = get_rule("readme")
    registry = get_rule("registry")
    diagnostics = [
        Diagnostic("m", file=pkg / "a.py", line=1, rule=readme),
        Diagnostic("m", file=pkg / "a.py", line=1, severity="warning", rule=registry),
        Diagnostic("m", file=pkg / "data" / "b.py", line=1, rule=registry),
    ]
    config = PRESETS["template"]
    config = type(config)(
        **{**config.__dict__, "per_file_ignores": (("src/e/data/**", ("IEFS004",)),)}
    )
    apply_suppressions(diagnostics, load_suppressions(pkg), config, tmp_path)
    assert [d.status for d in diagnostics] == ["suppressed", "warn", "suppressed"]
