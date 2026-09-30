"""Ignore comments and path-based ignores."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint import LintConfig, lint_package
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


def test_file_level_outside_header_is_a_problem_not_a_suppression(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "late.py", "\n" * 12 + "# inspect-evals-lint: ignore-file[readme]\n")
    s = load_suppressions(context_for(pkg))
    assert s.file_level == {}
    assert [(p.file.name, p.line) for p in s.problems] == [("late.py", 13)]
    assert "first 10 lines" in s.problems[0].message


@pytest.mark.parametrize(
    "comment",
    [
        "# inspect-evals-lint: ignore",
        "# inspect-evals-lint: ignore[]",
        "# inspect-evals-lint: ignore-file",
    ],
)
def test_ignore_without_a_rule_is_a_problem(tmp_path: Path, comment: str) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", f"x = 1  {comment}\n")
    s = load_suppressions(context_for(pkg))
    assert s.line_level == {}
    assert s.file_level == {}
    assert [p.message for p in s.problems] == [
        "an ignore comment must name at least one rule, so this one suppresses nothing"
    ]
    assert s.problems[0].line == 1


def test_unknown_selector_is_a_problem_and_the_known_ones_still_apply(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", "x = 1  # inspect-evals-lint: ignore[readme, NOPE, IEZZ]\n")
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "x.py"] == {1: {"readme"}}
    assert [p.message for p in s.problems] == [
        "'IEZZ' names no rule, so this selector suppresses nothing",
        "'NOPE' names no rule, so this selector suppresses nothing",
    ]
    assert s.comments == 1


@pytest.mark.parametrize(
    "setup",
    ["comment", "file-comment", "dotfile"],
)
def test_legacy_syntax_is_a_problem_naming_the_replacement(tmp_path: Path, setup: str) -> None:
    pkg = tmp_path / "e"
    if setup == "comment":
        write(pkg / "x.py", "x = 1  # noautolint: readme\n")
    elif setup == "file-comment":
        write(pkg / "x.py", "# noautolint-file: readme\n")
    else:
        write(pkg / ".noautolint", "readme\n")
    s = load_suppressions(context_for(pkg))
    (problem,) = s.problems
    assert "no longer read" in problem.message
    assert "ignore[<rule>]" in problem.hint or "per-file-ignores" in problem.hint
    assert problem.line == (None if setup == "dotfile" else 1)
    assert s.line_level == {}
    assert s.file_level == {}


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
    config = PRESETS["multi-eval"]
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


def test_compose_comment_covers_its_own_line(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "challenges" / "compose.yaml",
        "services:\n"
        "  default:\n"
        "    image: example/untagged  # inspect-evals-lint: ignore[IEBP005]\n"
        "    privileged: true  # inspect-evals-lint: ignore[sandbox_privileges]\n"
        "    cap_add:\n"
        "      - SYS_PTRACE  # inspect-evals-lint: ignore[IESC001]\n"
        "      - SYS_ADMIN\n",
    )
    config = replace(config, exclude=("src/alpha/challenges/**",))
    report = lint_package(root, "alpha", config)
    statuses = report.statuses()
    assert statuses["sandbox_image_pinning"] == ["suppressed"]
    assert sorted(statuses["sandbox_privileges"]) == ["fail", "suppressed", "suppressed"]


def test_dockerfile_legacy_comment_is_a_problem(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "Dockerfile.gpu", "FROM x\n# noautolint: readme\n")
    s = load_suppressions(context_for(pkg))
    assert [(p.file.name, p.line) for p in s.problems] == [("Dockerfile.gpu", 2)]


def test_comments_in_excluded_files_are_not_read(tmp_path: Path) -> None:
    """A legacy or malformed comment in sandbox code that exclude keeps out of the rules is not an error."""
    from dataclasses import replace

    pkg = tmp_path / "src" / "e"
    write(pkg / "challenges" / "solve.py", "print 'py2'  # noautolint: readme\n")
    write(pkg / "challenges" / "Dockerfile", "# noautolint: readme\nFROM x\n")
    write(pkg / "challenges" / ".noautolint", "readme\n")
    write(pkg / "ok.py", "x = 1  # inspect-evals-lint: ignore[readme]\n")
    assert len(load_suppressions(context_for(pkg)).problems) == 3
    config = replace(PRESETS["multi-eval"], exclude=("e/challenges/**",))  # root is pkg.parent
    s = load_suppressions(context_for(pkg, config))
    assert list(s.line_level) == [pkg / "ok.py"]
    assert s.problems == []


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


@pytest.mark.parametrize(
    ("comment", "level"),
    [
        ("x = 1  # inspect-evals-lint: ignore[readme] -- the README lives upstream", "line"),
        ("x = 1  # inspect-evals-lint: ignore[readme]--terse", "line"),
        ("# inspect-evals-lint: ignore-file[readme] -- generated file", "file"),
    ],
)
def test_reason_after_a_double_dash_is_read(tmp_path: Path, comment: str, level: str) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", comment + "\n")
    s = load_suppressions(context_for(pkg))
    assert s.missing_reasons == []
    assert s.problems == []
    if level == "line":
        assert s.line_level[pkg / "x.py"] == {1: {"readme"}}
    else:
        assert s.file_level[pkg / "x.py"] == {"readme"}


@pytest.mark.parametrize(
    "comment",
    [
        "x = 1  # inspect-evals-lint: ignore[readme, IEBP003]",
        "x = 1  # inspect-evals-lint: ignore[readme, IEBP003] because upstream",
        "x = 1  # inspect-evals-lint: ignore[readme, IEBP003] --   ",
        "# inspect-evals-lint: ignore-file[readme, IEBP003]",
    ],
)
def test_suppression_without_a_reason_still_applies_and_is_recorded(
    tmp_path: Path, comment: str
) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", comment + "\n")
    s = load_suppressions(context_for(pkg))
    selectors = s.file_level.get(pkg / "x.py") or s.line_level[pkg / "x.py"][1]
    assert selectors == {"readme", "IEBP003"}
    (missing,) = s.missing_reasons
    assert (missing.file.name, missing.line) == ("x.py", 1)
    assert "gives no reason" in missing.message
    assert "[readme, IEBP003] -- <reason>`" in missing.hint


def test_a_comment_that_suppresses_nothing_needs_no_reason(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", "x = 1  # inspect-evals-lint: ignore[NOPE]\n")
    s = load_suppressions(context_for(pkg))
    assert s.missing_reasons == []
    assert len(s.problems) == 1


def test_dockerfile_comment_with_a_reason(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(
        pkg / "Dockerfile",
        "# inspect-evals-lint: ignore[dockerfile_locking] -- base image pins its own lock\n"
        "FROM python:3.12\n",
    )
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "Dockerfile"] == {2: {"dockerfile_locking"}}
    assert s.missing_reasons == []


@pytest.mark.parametrize(
    "reason",
    [
        "was # noautolint: readme",
        "rather than # inspect-evals-lint: ignore-file[IEBP]",
        "not # inspect-evals-lint: ignore",
    ],
)
def test_the_reason_is_not_read_for_other_markers(tmp_path: Path, reason: str) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", f"x = 1  # inspect-evals-lint: ignore[readme] -- {reason}\n")
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "x.py"] == {1: {"readme"}}
    assert s.file_level == {}
    assert s.problems == []
    assert s.missing_reasons == []


def test_a_second_marker_before_any_reason_is_read_too(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(
        pkg / "x.py",
        "x = 1  # inspect-evals-lint: ignore[readme]  # inspect-evals-lint: ignore[sample_ids] -- why\n",
    )
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "x.py"] == {1: {"readme", "sample_ids"}}
    assert [m.message for m in s.missing_reasons] == ["ignore[readme] gives no reason"]
    assert s.comments == 2


@pytest.mark.parametrize(
    "tail",
    ["--  # noqa: E501", "-- --", "-- <reason>", "-- <why this is acceptable>", "-- #"],
)
def test_placeholders_and_other_comments_are_not_reasons(tmp_path: Path, tail: str) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", f"x = 1  # inspect-evals-lint: ignore[readme] {tail}\n")
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "x.py"] == {1: {"readme"}}
    assert len(s.missing_reasons) == 1


def test_a_reason_may_be_followed_by_another_tool_s_comment(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", "x = 1  # inspect-evals-lint: ignore[readme] -- upstream  # noqa: E501\n")
    s = load_suppressions(context_for(pkg))
    assert s.missing_reasons == []


def test_the_hint_rewrites_the_comment_with_only_the_selectors_that_apply(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", "x = 1  # inspect-evals-lint: ignore[readme, NOPE, IEBP003]\n")
    s = load_suppressions(context_for(pkg))
    (missing,) = s.missing_reasons
    assert missing.message == "ignore[readme, IEBP003] gives no reason"
    assert "`# inspect-evals-lint: ignore[readme, IEBP003] -- <reason>`" in missing.hint


def test_a_past_header_ignore_file_needs_no_reason(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(pkg / "x.py", "\n" * 12 + "# inspect-evals-lint: ignore-file[readme]\n")
    s = load_suppressions(context_for(pkg))
    assert len(s.problems) == 1
    assert s.missing_reasons == []


def test_dockerfile_missing_reason_is_reported_at_the_comment(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(
        pkg / "Dockerfile", "# inspect-evals-lint: ignore[dockerfile_locking]\nFROM python:3.12\n"
    )
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "Dockerfile"] == {2: {"dockerfile_locking"}}
    assert [(m.file.name, m.line) for m in s.missing_reasons] == [("Dockerfile", 1)]


def test_compose_comments_take_a_reason(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    write(
        pkg / "compose.yaml",
        "services:\n"
        "  default:\n"
        "    privileged: true  # inspect-evals-lint: ignore[IESC001] -- needs KVM\n"
        "    cap_add: [SYS_PTRACE]  # inspect-evals-lint: ignore[IESC001]\n",
    )
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "compose.yaml"] == {3: {"IESC001"}, 4: {"IESC001"}}
    assert [m.line for m in s.missing_reasons] == [4]


def test_crlf_and_tabs(tmp_path: Path) -> None:
    pkg = tmp_path / "e"
    (pkg).mkdir()
    (pkg / "x.py").write_bytes(b"x = 1\t# inspect-evals-lint: ignore[readme]\t--\twhy\r\n")
    s = load_suppressions(context_for(pkg))
    assert s.line_level[pkg / "x.py"] == {1: {"readme"}}
    assert s.missing_reasons == []


@pytest.mark.parametrize(
    "comment",
    [
        "x = 1  # inspect-evals-lint: ignore[IECQ]",
        "x = 1  # inspect-evals-lint: ignore[readme, suppression_syntax]",
        "# inspect-evals-lint: ignore-file[IECQ005]",
    ],
)
def test_a_comment_cannot_suppress_reports_about_suppression_comments(
    tmp_path: Path, comment: str
) -> None:
    from inspect_evals_lint.rules.code_quality import suppression_syntax

    pkg = tmp_path / "e"
    write(pkg / "x.py", comment + "\n")
    ctx = context_for(pkg)
    results = [r for r in suppression_syntax(ctx) if isinstance(r, Diagnostic)]
    for result in results:
        result.rule = get_rule("suppression_syntax")
    apply_suppressions(results, load_suppressions(ctx), ctx.config, tmp_path)
    assert [r.status for r in results] == ["warn"]
