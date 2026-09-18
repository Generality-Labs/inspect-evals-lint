"""File-structure rules: package location, main file, exports, registry, eval.yaml, README."""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import yaml

from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.context import LintContext, is_package
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
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
    name="package_location",
    category="file_structure",
    scopes=("eval", "helper"),
    summary="The package exists at <source-root>/<name>/ with an __init__.py",
)
def package_location(ctx: LintContext) -> Iterable[Finding]:
    """Check the package lives at ``<source_root>/<name>``.

    Every other rule depends on this one. A directory that exists but has no
    ``__init__.py`` is documentation or data, not code, and is skipped rather
    than failed; the same rule keeps it out of discovery.
    """
    if is_package(ctx.path):
        yield Outcome("pass", f"Package located at {ctx.path.relative_to(ctx.root).as_posix()}")
        return
    location = f"{ctx.config.source_root}/{ctx.name}"
    if ctx.path.is_dir():
        yield Outcome(
            "skip",
            f"{location} has no __init__.py, so it is not a Python package; "
            "documentation-only directories are not linted",
        )
        return
    yield Diagnostic(f"Evaluation directory not found: {location}", file=ctx.path)


MAIN_FILE_ALTERNATIVE = "tasks.py"
"""Also accepted as the module holding the ``@task`` functions, alongside ``<name>.py``."""


def main_file_candidates(package_path: Path, name: str) -> tuple[Path, ...]:
    return (package_path / f"{name}.py", package_path / MAIN_FILE_ALTERNATIVE)


def find_main_file(package_path: Path, name: str) -> Path:
    """The module the rules treat as the evaluation's main file.

    The first candidate that defines a ``@task`` wins, then the first that exists,
    so a stray empty ``<name>.py`` does not hide the tasks in ``tasks.py``.
    Falls back to ``<name>.py`` when neither exists, for the failure message.
    """
    candidates = main_file_candidates(package_path, name)
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
def main_file(ctx: LintContext) -> Iterable[Finding]:
    """Check ``<name>.py`` or ``tasks.py`` exists with at least one ``@task``.

    Keeping tasks in a predictably named module lets tooling and readers find
    them. When both files exist, the first that defines a task is used,
    ``<name>.py`` first.
    """
    target = find_main_file(ctx.path, ctx.name)

    if not target.exists():
        expected = " or ".join(c.name for c in main_file_candidates(ctx.path, ctx.name))
        yield Diagnostic(f"Missing main file: {expected}", file=target)
        return

    try:
        ast.parse(target.read_text(encoding="utf-8"))
    except SyntaxError as e:
        yield Diagnostic(f"Syntax error in {target.name}: {e}", file=target, line=e.lineno)
        return

    task_functions = _find_task_functions(target)
    if not task_functions:
        yield Diagnostic(f"{target.name} has no @task decorated functions", file=target)
        return

    yield Outcome(
        "pass", f"{target.name} has {len(task_functions)} @task function(s): {task_functions}"
    )


@rule(
    code="IEFS003",
    name="init_exports",
    category="file_structure",
    summary="__init__.py exports every @task function from the main file",
)
def init_exports(ctx: LintContext) -> Iterable[Finding]:
    """Check ``__init__.py`` re-exports every ``@task`` function from the main file, via ``__all__`` or ``from ... import``."""
    init_file = ctx.path / "__init__.py"
    target = find_main_file(ctx.path, ctx.name)

    if not init_file.exists():
        yield Diagnostic("Missing __init__.py file", file=init_file)
        return

    try:
        ast.parse(init_file.read_text(encoding="utf-8"))
    except SyntaxError as e:
        yield Diagnostic(f"Syntax error in __init__.py: {e}", file=init_file, line=e.lineno)
        return

    if not target.exists():
        yield Outcome("skip", f"Main file {target.name} not found, cannot check exports")
        return

    task_functions = _find_task_functions(target)
    if not task_functions:
        yield Outcome("skip", "No task functions to check for exports")
        return

    exported_names = _get_exported_names(init_file)
    missing = [fn for fn in task_functions if fn not in exported_names]
    for fn in missing:
        yield Diagnostic(
            f"__init__.py does not export the @task function {fn!r}",
            file=init_file,
            hint=f"add `from .{target.stem} import {fn}` and list it in __all__",
        )
    if not missing:
        yield Outcome("pass", f"__init__.py exports all {len(task_functions)} task function(s)")


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


def _registry_module(repo_root: Path, name: str, config: LintConfig) -> Iterable[Finding]:
    registry_file = repo_root / (config.registry_module or "")
    if not registry_file.exists():
        yield Outcome("skip", f"{registry_file.name} not found")
        return

    module = config.module_name(name)
    pattern = rf"from {re.escape(module)}\b|^\s*import {re.escape(module)}\b"
    if re.search(pattern, registry_file.read_text(encoding="utf-8"), re.MULTILINE):
        yield Outcome("pass", f"Evaluation registered in {registry_file.name}")
    else:
        yield Diagnostic(
            f"Evaluation not imported in {registry_file.name}",
            file=registry_file,
            hint=f"add `from {module} import ...`",
        )


def _registry_entry_points(repo_root: Path, name: str, config: LintConfig) -> Iterable[Finding]:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        yield Outcome("skip", "pyproject.toml not found")
        return

    module = config.module_name(name)
    entry_points = _entry_point_modules(pyproject)
    registered = name in entry_points or any(
        value == module or value.startswith((module + ".", module + ":"))
        for value in entry_points.values()
    )
    if registered:
        yield Outcome(
            "pass",
            "Evaluation registered in pyproject.toml under [project.entry-points.inspect_ai]",
        )
    else:
        yield Diagnostic(
            "Evaluation not registered",
            file=pyproject,
            hint=f'add to pyproject.toml: [project.entry-points.inspect_ai]\n{name} = "{module}"',
        )


@rule(
    code="IEFS004",
    name="registry",
    category="file_structure",
    summary="The evaluation is registered so inspect eval can find its tasks",
)
def registry(ctx: LintContext) -> Iterable[Finding]:
    """Check the evaluation is registered so ``inspect eval`` can find its tasks.

    Under ``[project.entry-points.inspect_ai]`` in ``pyproject.toml``
    (``registry = "entry-points"``), or imported by a registry module
    (``registry = "module"``). ``registry = "none"`` skips.
    """
    if ctx.config.registry == "none":
        yield Outcome("skip", 'Registry check disabled (registry = "none")')
    elif ctx.config.registry == "module":
        yield from _registry_module(ctx.root, ctx.name, ctx.config)
    else:
        yield from _registry_entry_points(ctx.root, ctx.name, ctx.config)


@rule(
    code="IEFS005",
    name="eval_yaml",
    category="file_structure",
    summary="eval.yaml exists, is a mapping, and defines the required fields",
)
def eval_yaml(ctx: LintContext) -> Iterable[Finding]:
    """Check ``eval.yaml`` exists, is a mapping, and defines the configured required fields.

    A missing file is a skip rather than a failure when ``eval-yaml-required`` is
    false, because the inspect_evals register entry carries the metadata for
    upstream repositories; a present file is validated either way.
    """
    eval_yaml_file = ctx.path / "eval.yaml"
    if not eval_yaml_file.exists():
        if ctx.config.eval_yaml_required:
            yield Diagnostic(
                f"Missing eval.yaml in {ctx.config.source_root}/{ctx.name}/", file=eval_yaml_file
            )
        else:
            yield Outcome("skip", "No eval.yaml; not required in this layout")
        return

    try:
        data = yaml.safe_load(eval_yaml_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        yield Diagnostic(f"eval.yaml is not valid YAML: {e}", file=eval_yaml_file)
        return

    if not isinstance(data, dict):
        yield Diagnostic("eval.yaml is not a mapping", file=eval_yaml_file)
        return

    missing = [name for name in ctx.config.eval_yaml_required_fields if name not in data]
    for name in missing:
        yield Diagnostic(f"eval.yaml is missing the required field {name!r}", file=eval_yaml_file)
    if not missing:
        yield Outcome("pass", "eval.yaml has all required fields")


@rule(
    code="IEFS006",
    name="readme",
    category="file_structure",
    summary="README.md exists and has no TODO markers",
)
def readme(ctx: LintContext) -> Iterable[Finding]:
    """Check ``README.md`` exists; warn on each line that still carries a ``TODO:`` marker.

    With ``readme-location = "repo-root"`` the repository's top-level
    ``README.md`` is accepted when the evaluation directory has none.
    """
    readme_file = ctx.path / "README.md"
    fallback = ctx.root / "README.md" if ctx.config.readme_location == "repo-root" else None
    if not readme_file.exists() and fallback is not None and fallback.exists():
        readme_file = fallback
    if not readme_file.exists():
        message = "Missing README.md"
        if fallback is not None:
            message += f" (looked in {ctx.path.name}/ and {fallback.parent.name}/)"
        yield Diagnostic(message, file=readme_file)
        return

    todos = [
        i
        for i, line in enumerate(readme_file.read_text(encoding="utf-8").splitlines(), start=1)
        if "TODO:" in line
    ]
    for line in todos:
        yield Diagnostic(
            "README.md still contains a TODO marker",
            file=readme_file,
            line=line,
            severity="warning",
        )
    if not todos:
        yield Outcome("pass", "README.md exists")
