"""Linting by task file: the layout the file implies, and lint_task_files on top of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspect_evals_lint import (
    PRESETS,
    TaskLayout,
    UnsupportedLayoutError,
    lint_task_files,
    task_layout,
    task_layouts,
)
from tests.conftest import make_register_repo, write


def test_package_under_src(tmp_path: Path) -> None:
    write(tmp_path / "src/castle/__init__.py", "")
    write(tmp_path / "src/castle/castle.py", "")
    assert task_layout(tmp_path, "src/castle/castle.py") == TaskLayout("castle", "src", "")


def test_package_at_repo_root(tmp_path: Path) -> None:
    write(tmp_path / "bench/__init__.py", "")
    write(tmp_path / "bench/bench.py", "")
    assert task_layout(tmp_path, "bench/bench.py") == TaskLayout("bench", ".", "")


def test_nested_packages_give_an_import_prefix(tmp_path: Path) -> None:
    write(tmp_path / "india_evals/__init__.py", "")
    write(tmp_path / "india_evals/safeguards/__init__.py", "")
    write(tmp_path / "india_evals/safeguards/task.py", "")
    write(tmp_path / "src/concordia/__init__.py", "")
    write(tmp_path / "src/concordia/bixbench/__init__.py", "")
    write(tmp_path / "src/concordia/bixbench/bixbench.py", "")
    assert task_layout(tmp_path, "india_evals/safeguards/task.py") == TaskLayout(
        "safeguards", "india_evals", "india_evals"
    )
    assert task_layout(tmp_path, "src/concordia/bixbench/bixbench.py") == TaskLayout(
        "bixbench", "src/concordia", "concordia"
    )


def test_two_task_files_in_one_package_give_one_layout(tmp_path: Path) -> None:
    write(tmp_path / "src/x/__init__.py", "")
    write(tmp_path / "src/x/a.py", "")
    write(tmp_path / "src/x/b.py", "")
    assert task_layouts(tmp_path, ["src/x/a.py", "src/x/b.py", "src/x/a.py"]) == [
        TaskLayout("x", "src", "")
    ]


@pytest.mark.parametrize(
    ("files", "task_path", "message"),
    [
        (["bare.py"], "bare.py", "not inside a package"),
        (["src/module.py"], "src/module.py", "not inside a package"),
        ([], "src/x/x.py", "not found"),
        ([], "../outside.py", "escapes"),
    ],
)
def test_unsupported_layouts_name_the_reason(
    tmp_path: Path, files: list[str], task_path: str, message: str
) -> None:
    for file in files:
        write(tmp_path / file, "")
    with pytest.raises(UnsupportedLayoutError, match=message):
        task_layout(tmp_path, task_path)


def test_layout_config_keeps_everything_but_the_layout() -> None:
    config = TaskLayout("inner", "src/pkg", "pkg").config(PRESETS["register"])
    assert (config.source_root, config.import_prefix) == ("src/pkg", "pkg")
    assert config.tests_layout == "flat"  # the register preset's own setting survives
    assert config.eval_yaml_required is False


def test_lint_task_files_uses_the_register_preset_by_default(tmp_path: Path) -> None:
    make_register_repo(tmp_path, "alpha")
    run = lint_task_files(tmp_path, ["src/alpha/alpha.py"])
    assert [p.name for p in run.packages] == ["alpha"]
    assert run.passed(), [d.message for p in run.packages for d in p.diagnostics]
    assert run.root == tmp_path


def test_lint_task_files_lints_one_package_per_layout(tmp_path: Path) -> None:
    make_register_repo(tmp_path, "alpha")
    write(tmp_path / "src/beta/__init__.py", "")
    write(tmp_path / "src/beta/beta.py", "")
    run = lint_task_files(
        tmp_path, ["src/alpha/alpha.py", "src/beta/beta.py", "src/alpha/alpha.py"]
    )
    assert [p.name for p in run.packages] == ["alpha", "beta"]
    assert not run.packages[1].passed()  # beta has no task, README or tests


def test_lint_task_files_raises_for_a_bare_module(tmp_path: Path) -> None:
    make_register_repo(tmp_path, "alpha")
    write(tmp_path / "task.py", "")
    with pytest.raises(UnsupportedLayoutError, match="not inside a package"):
        lint_task_files(tmp_path, ["src/alpha/alpha.py", "task.py"])
