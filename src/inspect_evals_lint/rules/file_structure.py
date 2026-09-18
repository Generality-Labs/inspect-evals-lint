"""File-structure checks: eval location, main file, exports, registry, eval.yaml, README."""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import yaml

from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.context import LintContext, get_eval_path
from inspect_evals_lint.models import LintResult
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import get_decorator_name


def _find_task_functions(file_path: Path) -> list[str]:
    """Names of ``@task``-decorated functions in ``file_path`` (empty if missing or unparsable)."""
    if not file_path.exists():
        return []
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
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
        tree = ast.parse(init_file.read_text(encoding="utf-8"))
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


@rule(
    code="IEFS001",
    name="eval_location",
    category="file_structure",
    scopes=("eval", "helper"),
    summary="The package exists at <source-root>/<name>/ with an __init__.py",
)
def eval_location(ctx: LintContext) -> Iterable[LintResult]:
    """Check the package lives at ``<source_root>/<eval_name>``; returns its path.

    A directory that exists but has no ``__init__.py`` is skipped rather than
    failed: it is documentation or data, not code, and the same rule keeps it
    out of ``--all-evals`` discovery.
    """
    repo_root, eval_name, config = ctx.root, ctx.name, ctx.config
    eval_path = get_eval_path(repo_root, eval_name, config)
    if eval_path:
        yield LintResult(
            name="eval_location",
            status="pass",
            message=f"Package located at {eval_path.relative_to(repo_root).as_posix()}",
        )

        return
    location = f"{config.source_root}/{eval_name}"
    if config.eval_dir(repo_root, eval_name).is_dir():
        yield LintResult(
            name="eval_location",
            status="skip",
            message=(
                f"{location} has no __init__.py, so it is not a Python package; "
                "documentation-only directories are not linted"
            ),
        )

        return
    yield LintResult(
        name="eval_location",
        status="fail",
        message=f"Evaluation directory not found: {location}",
    )

    return None


MAIN_FILE_ALTERNATIVE = "tasks.py"
"""Also accepted as the module holding the ``@task`` functions, alongside ``<eval_name>.py``."""


def main_file_candidates(eval_path: Path, eval_name: str) -> tuple[Path, ...]:
    return (eval_path / f"{eval_name}.py", eval_path / MAIN_FILE_ALTERNATIVE)


def find_main_file(eval_path: Path, eval_name: str) -> Path:
    """The module the checks treat as the evaluation's main file.

    The first candidate that defines a ``@task`` wins, then the first that exists,
    so a stray empty ``<eval_name>.py`` does not hide the tasks in ``tasks.py``.
    Falls back to ``<eval_name>.py`` when neither exists, for the failure message.
    """
    candidates = main_file_candidates(eval_path, eval_name)
    for candidate in candidates:
        if _find_task_functions(candidate):
            return candidate
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


@rule(
    code="IEFS002",
    name="main_file",
    category="file_structure",
    summary="<name>.py or tasks.py exists and defines at least one @task function",
)
def main_file(ctx: LintContext) -> Iterable[LintResult]:
    """Check ``<eval_name>.py`` or ``tasks.py`` exists with at least one ``@task``; returns the task names."""
    eval_path, eval_name = ctx.path, ctx.name
    main_file = find_main_file(eval_path, eval_name)

    if not main_file.exists():
        expected = " or ".join(c.name for c in main_file_candidates(eval_path, eval_name))
        yield LintResult(
            name="main_file",
            status="fail",
            message=f"Missing main file: {expected}",
            file=str(main_file),
        )

        return

    try:
        ast.parse(main_file.read_text(encoding="utf-8"))
    except SyntaxError as e:
        yield LintResult(
            name="main_file",
            status="fail",
            message=f"Syntax error in {main_file.name}: {e}",
            file=str(main_file),
        )

        return

    task_functions = _find_task_functions(main_file)
    if not task_functions:
        yield LintResult(
            name="main_file",
            status="fail",
            message=f"{main_file.name} has no @task decorated functions",
            file=str(main_file),
        )

        return

    yield LintResult(
        name="main_file",
        status="pass",
        message=f"{main_file.name} has {len(task_functions)} @task function(s): {task_functions}",
        file=str(main_file),
    )

    return task_functions


@rule(
    code="IEFS003",
    name="init_exports",
    category="file_structure",
    summary="__init__.py exports every @task function from the main file",
)
def init_exports(ctx: LintContext) -> Iterable[LintResult]:
    """Check ``__init__.py`` re-exports every ``@task`` function from the main file."""
    eval_path, eval_name = ctx.path, ctx.name
    init_file = eval_path / "__init__.py"
    main_file = find_main_file(eval_path, eval_name)

    if not init_file.exists():
        yield LintResult(
            name="init_exports",
            status="fail",
            message="Missing __init__.py file",
            file=str(init_file),
        )

        return

    try:
        ast.parse(init_file.read_text(encoding="utf-8"))
    except SyntaxError as e:
        yield LintResult(
            name="init_exports",
            status="fail",
            message=f"Syntax error in __init__.py: {e}",
            file=str(init_file),
        )

        return

    if not main_file.exists():
        yield LintResult(
            name="init_exports",
            status="skip",
            message=f"Main file {main_file.name} not found, cannot check exports",
            file=str(init_file),
        )

        return

    task_functions = _find_task_functions(main_file)
    if not task_functions:
        yield LintResult(
            name="init_exports",
            status="skip",
            message="No task functions to check for exports",
            file=str(init_file),
        )

        return

    exported_names = _get_exported_names(init_file)
    missing_exports = [fn for fn in task_functions if fn not in exported_names]
    if missing_exports:
        yield LintResult(
            name="init_exports",
            status="fail",
            message=f"__init__.py does not export task functions: {missing_exports}",
            file=str(init_file),
        )

    else:
        yield LintResult(
            name="init_exports",
            status="pass",
            message=f"__init__.py exports all {len(task_functions)} task function(s)",
            file=str(init_file),
        )


def _table(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value: Any = mapping.get(key)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _entry_point_modules(pyproject: Path) -> dict[str, str]:
    """``[project.entry-points.inspect_ai]`` as ``{name: module}``."""
    try:
        data: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    group = _table(_table(_table(data, "project"), "entry-points"), "inspect_ai")
    return {str(k): str(v) for k, v in group.items()}


def _check_registry_module(
    repo_root: Path, eval_name: str, config: LintConfig
) -> Iterable[LintResult]:
    registry_file = repo_root / (config.registry_module or "")
    display = registry_file.name
    if not registry_file.exists():
        yield LintResult(name="registry", status="skip", message=f"{display} not found")
        return

    module = config.module_name(eval_name)
    pattern = rf"from {re.escape(module)}\b|^\s*import {re.escape(module)}\b"
    if re.search(pattern, registry_file.read_text(encoding="utf-8"), re.MULTILINE):
        yield LintResult(
            name="registry",
            status="pass",
            message=f"Evaluation registered in {display}",
            file=str(registry_file),
        )

    else:
        yield LintResult(
            name="registry",
            status="fail",
            message=f"Evaluation not imported in {display} (expected: from {module} import ...)",
            file=str(registry_file),
        )


def _check_registry_entry_points(
    repo_root: Path, eval_name: str, config: LintConfig
) -> Iterable[LintResult]:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        yield LintResult(name="registry", status="skip", message="pyproject.toml not found")
        return

    module = config.module_name(eval_name)
    entry_points = _entry_point_modules(pyproject)
    registered = eval_name in entry_points or any(
        value == module or value.startswith((module + ".", module + ":"))
        for value in entry_points.values()
    )
    if registered:
        yield LintResult(
            name="registry",
            status="pass",
            message="Evaluation registered in pyproject.toml under [project.entry-points.inspect_ai]",
            file=str(pyproject),
        )

    else:
        yield LintResult(
            name="registry",
            status="fail",
            message=(
                "Evaluation not registered. Add to pyproject.toml: "
                f'[project.entry-points.inspect_ai]\n{eval_name} = "{module}"'
            ),
            file=str(pyproject),
        )


@rule(
    code="IEFS004",
    name="registry",
    category="file_structure",
    summary="The evaluation is registered so inspect eval can find its tasks",
)
def registry(ctx: LintContext) -> Iterable[LintResult]:
    """Check the evaluation is registered so ``inspect eval`` can find its tasks."""
    repo_root, eval_name, config = ctx.root, ctx.name, ctx.config
    if config.registry == "none":
        yield LintResult(
            name="registry",
            status="skip",
            message='Registry check disabled (registry = "none")',
        )

    elif config.registry == "module":
        yield from _check_registry_module(repo_root, eval_name, config)
    else:
        yield from _check_registry_entry_points(repo_root, eval_name, config)


@rule(
    code="IEFS005",
    name="eval_yaml",
    category="file_structure",
    summary="eval.yaml exists, is a mapping, and defines the required fields",
)
def eval_yaml(ctx: LintContext) -> Iterable[LintResult]:
    """Check ``eval.yaml`` exists, is a mapping, and defines the configured required fields.

    A missing file is a skip rather than a failure when ``config.eval_yaml_required`` is
    false; a present file is validated either way.
    """
    repo_root, eval_name, config = ctx.root, ctx.name, ctx.config
    eval_yaml_file = config.eval_dir(repo_root, eval_name) / "eval.yaml"
    if not eval_yaml_file.exists():
        if config.eval_yaml_required:
            yield LintResult(
                name="eval_yaml",
                status="fail",
                message=f"Missing eval.yaml in {config.source_root}/{eval_name}/",
                file=str(eval_yaml_file),
            )

        else:
            yield LintResult(
                name="eval_yaml",
                status="skip",
                message="No eval.yaml; not required in this layout",
            )

        return

    try:
        data = yaml.safe_load(eval_yaml_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        yield LintResult(
            name="eval_yaml",
            status="fail",
            message=f"eval.yaml is not valid YAML: {e}",
            file=str(eval_yaml_file),
        )

        return

    if not isinstance(data, dict):
        yield LintResult(
            name="eval_yaml",
            status="fail",
            message="eval.yaml is not a mapping",
            file=str(eval_yaml_file),
        )

        return

    missing_fields = [name for name in config.eval_yaml_required_fields if name not in data]
    if missing_fields:
        yield LintResult(
            name="eval_yaml",
            status="fail",
            message=f"eval.yaml missing fields: {missing_fields}",
            file=str(eval_yaml_file),
        )

    else:
        yield LintResult(
            name="eval_yaml",
            status="pass",
            message="eval.yaml has all required fields",
            file=str(eval_yaml_file),
        )


@rule(
    code="IEFS006",
    name="readme",
    category="file_structure",
    summary="README.md exists and has no TODO markers",
)
def readme(ctx: LintContext) -> Iterable[LintResult]:
    """Check ``README.md`` exists; warn if it still contains ``TODO:`` markers.

    ``fallback`` is a second acceptable location (the repository root's README) used
    when the evaluation directory has none.
    """
    eval_path = ctx.path
    fallback = ctx.root / "README.md" if ctx.config.readme_location == "repo-root" else None
    readme_file = eval_path / "README.md"
    if not readme_file.exists() and fallback is not None and fallback.exists():
        readme_file = fallback
    if not readme_file.exists():
        message = "Missing README.md"
        if fallback is not None:
            message += f" (looked in {eval_path.name}/ and {fallback.parent.name}/)"
        yield LintResult(
            name="readme",
            status="fail",
            message=message,
            file=str(readme_file),
        )

        return

    if "TODO:" in readme_file.read_text(encoding="utf-8"):
        yield LintResult(
            name="readme",
            status="warn",
            message="README.md contains TODO markers",
            file=str(readme_file),
        )

    else:
        yield LintResult(
            name="readme",
            status="pass",
            message="README.md exists",
            file=str(readme_file),
        )
