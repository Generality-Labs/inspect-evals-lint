"""Ignore comments and path-based ignores."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspect_evals_lint import ConfigError, LintConfig, lint_package
from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
from tests.conftest import context_for, write


def test_line_level_by_name_or_code(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.package_dir(root, "alpha")
    write(
        eval_dir / "private.py",
        "from inspect_ai.model._model import a  # inspect-evals-lint: ignore[private_api_imports]\n"
        "from inspect_ai.solver._solver import b  # inspect-evals-lint: ignore[IECQ001]\n"
        "from inspect_ai.tool._tool import c  # inspect-evals-lint: ignore[IECQ]\n"
        "from inspect_ai.util._util import d  # inspect-evals-lint: ignore[readme, IEBP]\n"
        "from inspect_ai.log._log import e\n",
    )
    report = lint_package(root, "alpha", config, check="private_api_imports")
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
    s = load_suppressions(context_for(pkg))
    assert s.file_level[pkg / "early.py"] == {"readme", "sample_ids"}


def test_file_level_outside_header_is_an_error(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "late.py", "\n" * 12 + "# inspect-evals-lint: ignore-file[readme]\n")
    with pytest.raises(ConfigError, match="first 10 lines"):
        load_suppressions(context_for(pkg))


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
        load_suppressions(context_for(pkg))


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
        load_suppressions(context_for(pkg))


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
    apply_suppressions(diagnostics, load_suppressions(context_for(pkg)), config, tmp_path)
    assert [d.status for d in diagnostics] == ["suppressed", "warn", "suppressed"]


def test_dockerfile_comment_covers_the_instruction_below_or_its_own_line(tmp_path: Path) -> None:
    """Dockerfile instructions take no trailing comment, so the comment above an instruction covers it."""
    pkg = tmp_path / "e"
    dockerfile = write(
        pkg / "Dockerfile",
        "# inspect-evals-lint: ignore-file[IEBP005]\n"
        "# inspect-evals-lint: ignore[readme]\n"
        "\n"
        "# an ordinary comment in between\n"
        "FROM python:3.12\n"
        "RUN pip install x  # inspect-evals-lint: ignore[IEBP, sample_ids]\n"
        "# inspect-evals-lint: ignore[IEFS001]\n",
    )
    write(pkg / "dockerfile.py", "x = 1  # inspect-evals-lint: ignore[IEFS002]\n")
    s = load_suppressions(context_for(pkg))
    assert s.file_level == {dockerfile: {"IEBP005"}}
    assert s.line_level[dockerfile] == {5: {"readme"}, 6: {"IEBP", "sample_ids"}, 7: {"IEFS001"}}
    assert s.line_level[pkg / "dockerfile.py"] == {1: {"IEFS002"}}


def test_dockerfile_legacy_comment_is_an_error(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "Dockerfile.gpu", "FROM x\n# noautolint: readme\n")
    with pytest.raises(ConfigError, match="ignore\\[<rule>\\]"):
        load_suppressions(context_for(pkg))


def test_comments_in_excluded_files_are_not_read(tmp_path: Path) -> None:
    """A legacy or malformed comment in sandbox code that exclude keeps out of the rules is not an error."""
    from dataclasses import replace

    pkg = tmp_path / "src" / "e"
    write(pkg / "challenges" / "solve.py", "print 'py2'  # noautolint: readme\n")
    write(pkg / "challenges" / "Dockerfile", "# noautolint: readme\nFROM x\n")
    write(pkg / "ok.py", "x = 1  # inspect-evals-lint: ignore[readme]\n")
    with pytest.raises(ConfigError):
        load_suppressions(context_for(pkg))
    config = replace(PRESETS["template"], exclude=("e/challenges/**",))  # root is pkg.parent
    s = load_suppressions(context_for(pkg, config))
    assert list(s.line_level) == [pkg / "ok.py"]


def test_comment_on_any_line_of_a_multiline_statement(monorepo: tuple[Path, LintConfig]) -> None:
    """Formatters move a trailing comment inside a parenthesised import; it still applies."""
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "private.py",
        "from inspect_ai._util.file import (\n"
        "    file,  # inspect-evals-lint: ignore[private_api_imports]\n"
        ")\n"
        "from inspect_ai.model._model import (  # inspect-evals-lint: ignore[IECQ001]\n"
        "    thing,\n"
        ")\n"
        "import definitely_not_installed_pkg  # inspect-evals-lint: ignore[external_dependencies]\n",
    )
    report = lint_package(root, "alpha", config)
    assert report.statuses()["private_api_imports"] == ["suppressed", "suppressed"]
    assert report.statuses()["external_dependencies"] == ["suppressed"]


def test_comment_inside_a_function_body_does_not_cover_its_definition(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "alpha.py",
        "from inspect_ai import task\n\n\n@task\ndef alpha(solver):\n"
        "    return None  # inspect-evals-lint: ignore[task_overridable_defaults]\n",
    )
    report = lint_package(root, "alpha", config, check="task_overridable_defaults")
    assert report.statuses()["task_overridable_defaults"] == ["fail"]
