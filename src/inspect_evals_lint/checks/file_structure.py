"""File-structure checks: eval location, main file, exports, registry, eval.yaml, README."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

from inspect_evals_lint.checks.utils import get_decorator_name
from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.models import LintReport, LintResult


def _find_task_functions(file_path: Path) -> list[str]:
    """Names of ``@task``-decorated functions in ``file_path`` (empty if missing or unparseable)."""
    if not file_path.exists():
        return []
    try:
        tree = ast.parse(file_path.read_text())
    except SyntaxError:
        return []
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(get_decorator_name(d) == "task" for d in node.decorator_list)
    ]


def _get_exported_names(init_file: Path) -> set[str]:
    """Names an ``__init__.py`` exposes via ``__all__`` or ``from ... import`` statements."""
    if not init_file.exists():
        return set()
    try:
        tree = ast.parse(init_file.read_text())
    except SyntaxError:
        return set()

    exported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__all__"
                    and isinstance(node.value, ast.List | ast.Tuple)
                ):
                    exported.update(
                        elt.value
                        for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    )
        if isinstance(node, ast.ImportFrom):
            exported.update(alias.asname or alias.name for alias in node.names)
    return exported


def get_eval_path(repo_root: Path, eval_name: str, config: LintConfig) -> Path | None:
    """The evaluation directory, or None if it does not exist."""
    eval_path = config.eval_dir(repo_root, eval_name)
    return eval_path if eval_path.is_dir() else None


def check_eval_location(
    repo_root: Path, eval_name: str, config: LintConfig, report: LintReport
) -> Path | None:
    """Check the evaluation lives at ``<source_root>/<eval_name>``; returns its path."""
    eval_path = get_eval_path(repo_root, eval_name, config)
    if eval_path:
        report.add(
            LintResult(
                name="eval_location",
                status="pass",
                message=f"Evaluation located at {eval_path.relative_to(repo_root)}",
            )
        )
        return eval_path
    report.add(
        LintResult(
            name="eval_location",
            status="fail",
            message=f"Evaluation directory not found: {config.source_root}/{eval_name}",
        )
    )
    return None


def check_main_file(eval_path: Path, eval_name: str, report: LintReport) -> list[str]:
    """Check ``<eval_name>.py`` exists with at least one ``@task``; returns the task names."""
    main_file = eval_path / f"{eval_name}.py"

    if not main_file.exists():
        report.add(
            LintResult(
                name="main_file",
                status="fail",
                message=f"Missing main file: {eval_name}.py",
                file=str(main_file),
            )
        )
        return []

    try:
        ast.parse(main_file.read_text())
    except SyntaxError as e:
        report.add(
            LintResult(
                name="main_file",
                status="fail",
                message=f"Syntax error in {eval_name}.py: {e}",
                file=str(main_file),
            )
        )
        return []

    task_functions = _find_task_functions(main_file)
    if not task_functions:
        report.add(
            LintResult(
                name="main_file",
                status="fail",
                message=f"{eval_name}.py has no @task decorated functions",
                file=str(main_file),
            )
        )
        return []

    report.add(
        LintResult(
            name="main_file",
            status="pass",
            message=f"{eval_name}.py has {len(task_functions)} @task function(s): {task_functions}",
            file=str(main_file),
        )
    )
    return task_functions


def check_init_exports(eval_path: Path, eval_name: str, report: LintReport) -> None:
    """Check ``__init__.py`` re-exports every ``@task`` function from the main file."""
    init_file = eval_path / "__init__.py"
    main_file = eval_path / f"{eval_name}.py"

    if not init_file.exists():
        report.add(
            LintResult(
                name="init_exports",
                status="fail",
                message="Missing __init__.py file",
                file=str(init_file),
            )
        )
        return

    try:
        ast.parse(init_file.read_text())
    except SyntaxError as e:
        report.add(
            LintResult(
                name="init_exports",
                status="fail",
                message=f"Syntax error in __init__.py: {e}",
                file=str(init_file),
            )
        )
        return

    if not main_file.exists():
        report.add(
            LintResult(
                name="init_exports",
                status="skip",
                message=f"Main file {eval_name}.py not found, cannot check exports",
                file=str(init_file),
            )
        )
        return

    task_functions = _find_task_functions(main_file)
    if not task_functions:
        report.add(
            LintResult(
                name="init_exports",
                status="skip",
                message="No task functions to check for exports",
                file=str(init_file),
            )
        )
        return

    exported_names = _get_exported_names(init_file)
    missing_exports = [fn for fn in task_functions if fn not in exported_names]
    if missing_exports:
        report.add(
            LintResult(
                name="init_exports",
                status="fail",
                message=f"__init__.py does not export task functions: {missing_exports}",
                file=str(init_file),
            )
        )
    else:
        report.add(
            LintResult(
                name="init_exports",
                status="pass",
                message=f"__init__.py exports all {len(task_functions)} task function(s)",
                file=str(init_file),
            )
        )


def _table(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value: Any = mapping.get(key)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _entry_point_modules(pyproject: Path) -> dict[str, str]:
    """``[project.entry-points.inspect_ai]`` as ``{name: module}``."""
    try:
        data: dict[str, Any] = tomllib.loads(pyproject.read_text())
    except tomllib.TOMLDecodeError:
        return {}
    group = _table(_table(_table(data, "project"), "entry-points"), "inspect_ai")
    return {str(k): str(v) for k, v in group.items()}


def _check_registry_module(
    repo_root: Path, eval_name: str, config: LintConfig, report: LintReport
) -> None:
    registry_file = repo_root / (config.registry_module or "")
    display = registry_file.name
    if not registry_file.exists():
        report.add(LintResult(name="registry", status="skip", message=f"{display} not found"))
        return

    module = config.module_name(eval_name)
    pattern = rf"from {re.escape(module)}\b|^\s*import {re.escape(module)}\b"
    if re.search(pattern, registry_file.read_text(), re.MULTILINE):
        report.add(
            LintResult(
                name="registry",
                status="pass",
                message=f"Evaluation registered in {display}",
                file=str(registry_file),
            )
        )
    else:
        report.add(
            LintResult(
                name="registry",
                status="fail",
                message=f"Evaluation not imported in {display} (expected: from {module} import ...)",
                file=str(registry_file),
            )
        )


def _check_registry_entry_points(
    repo_root: Path, eval_name: str, config: LintConfig, report: LintReport
) -> None:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        report.add(LintResult(name="registry", status="skip", message="pyproject.toml not found"))
        return

    module = config.module_name(eval_name)
    entry_points = _entry_point_modules(pyproject)
    registered = eval_name in entry_points or any(
        value == module or value.startswith((module + ".", module + ":"))
        for value in entry_points.values()
    )
    if registered:
        report.add(
            LintResult(
                name="registry",
                status="pass",
                message="Evaluation registered in pyproject.toml under [project.entry-points.inspect_ai]",
                file=str(pyproject),
            )
        )
    else:
        report.add(
            LintResult(
                name="registry",
                status="fail",
                message=(
                    "Evaluation not registered. Add to pyproject.toml: "
                    f'[project.entry-points.inspect_ai]\n{eval_name} = "{module}"'
                ),
                file=str(pyproject),
            )
        )


def check_registry(repo_root: Path, eval_name: str, config: LintConfig, report: LintReport) -> None:
    """Check the evaluation is registered so ``inspect eval`` can find its tasks."""
    if config.registry == "none":
        report.add(
            LintResult(
                name="registry",
                status="skip",
                message='Registry check disabled (registry = "none")',
            )
        )
    elif config.registry == "module":
        _check_registry_module(repo_root, eval_name, config, report)
    else:
        _check_registry_entry_points(repo_root, eval_name, config, report)


def check_eval_yaml(
    repo_root: Path, eval_name: str, config: LintConfig, report: LintReport
) -> None:
    """Check ``eval.yaml`` exists, is a mapping, and defines the configured required fields."""
    eval_yaml_file = config.eval_dir(repo_root, eval_name) / "eval.yaml"
    if not eval_yaml_file.exists():
        report.add(
            LintResult(
                name="eval_yaml",
                status="fail",
                message=f"Missing eval.yaml in {config.source_root}/{eval_name}/",
                file=str(eval_yaml_file),
            )
        )
        return

    try:
        data = yaml.safe_load(eval_yaml_file.read_text())
    except yaml.YAMLError as e:
        report.add(
            LintResult(
                name="eval_yaml",
                status="fail",
                message=f"eval.yaml is not valid YAML: {e}",
                file=str(eval_yaml_file),
            )
        )
        return

    if not isinstance(data, dict):
        report.add(
            LintResult(
                name="eval_yaml",
                status="fail",
                message="eval.yaml is not a mapping",
                file=str(eval_yaml_file),
            )
        )
        return

    missing_fields = [name for name in config.eval_yaml_required_fields if name not in data]
    if missing_fields:
        report.add(
            LintResult(
                name="eval_yaml",
                status="fail",
                message=f"eval.yaml missing fields: {missing_fields}",
                file=str(eval_yaml_file),
            )
        )
    else:
        report.add(
            LintResult(
                name="eval_yaml",
                status="pass",
                message="eval.yaml has all required fields",
                file=str(eval_yaml_file),
            )
        )


def check_readme(eval_path: Path, report: LintReport) -> None:
    """Check ``README.md`` exists; warn if it still contains ``TODO:`` markers."""
    readme_file = eval_path / "README.md"
    if not readme_file.exists():
        report.add(
            LintResult(
                name="readme",
                status="fail",
                message="Missing README.md",
                file=str(readme_file),
            )
        )
        return

    if "TODO:" in readme_file.read_text():
        report.add(
            LintResult(
                name="readme",
                status="warn",
                message="README.md contains TODO markers",
                file=str(readme_file),
            )
        )
    else:
        report.add(
            LintResult(
                name="readme",
                status="pass",
                message="README.md exists",
                file=str(readme_file),
            )
        )
