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
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import get_decorator_name, iter_python_files


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
    references=(
        inspect_docs("tasks", "Tasks: Packaging", "packaging"),
        inspect_docs(
            "extensions-components", "Extensions: Components: Registration", "registration"
        ),
    ),
)
def package_location(ctx: LintContext) -> Iterable[Finding]:
    """The package exists at ``<source-root>/<name>/`` with an ``__init__.py``.

    ## What it does
    Checks that the directory named on the command line, or found under
    ``source-root``, is a Python package. Every other rule depends on this one
    and does not run when it does not hold.

    ## Why is this bad?
    A directory that exists but has no ``__init__.py`` is documentation or data,
    not code: a README left where evaluations used to live, a fixtures folder. It
    is reported as a skip rather than a failure, and the same test keeps it out of
    ``--all`` discovery, so it needs no configuration.

    ## Example
    ```text
    src/inspect_evals/gdm_capabilities/README.md     # skipped: not a package
    src/inspect_evals/gpqa/__init__.py               # linted
    ```
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
    """The conventional homes for a package's ``@task`` functions, in order of preference."""
    return (package_path / f"{name}.py", package_path / MAIN_FILE_ALTERNATIVE)


def task_modules(ctx: LintContext) -> list[Path]:
    """Every module in the package that defines a ``@task``, conventional names first.

    ``<name>.py`` and ``tasks.py`` lead when they qualify, so a stray empty
    ``<name>.py`` does not hide the tasks in ``tasks.py``; any other module that
    defines a task follows in path order. Files the ``exclude`` globs rule out are
    not read.
    """
    candidates = main_file_candidates(ctx.path, ctx.name)
    files = iter_python_files(ctx)
    leading = [c for c in candidates if c in files and _find_task_functions(c)]
    others = [f for f in files if f not in candidates and _find_task_functions(f)]
    return leading + others


def _relative_module(ctx: LintContext, module: Path) -> str:
    """``.sub.mod`` for a module inside the package, as a relative import writes it."""
    return "." + ".".join(module.relative_to(ctx.path).with_suffix("").parts)


@rule(
    code="IEFS002",
    name="main_file",
    category="file_structure",
    summary="Some module defines a @task function, preferably <name>.py or tasks.py",
    references=(
        inspect_docs("tasks", "Tasks: Task Basics", "task-basics"),
        inspect_docs(
            "extensions-components", "Extensions: Components: Registration", "registration"
        ),
    ),
)
def main_file(ctx: LintContext) -> Iterable[Finding]:
    """Some module defines a ``@task`` function, preferably ``<name>.py`` or ``tasks.py``.

    ## What it does
    Reads every Python file in the package for ``@task`` functions. Passes when
    ``<name>.py`` or ``tasks.py`` defines one, preferring whichever does so a stray
    empty ``<name>.py`` does not hide the tasks in ``tasks.py``. Warns, once per
    module, when the tasks live only in other modules. Fails when no module defines
    a task, or when the conventional file exists but does not parse.

    ## Why is this bad?
    A package with no task is not an evaluation, whatever else it contains.
    Keeping the tasks in a predictably named module is a lesser matter, so it is a
    warning: it lets readers find them without opening every file, but a package
    that names its task module after the benchmark still runs.

    ## Example
    ```text
    src/my_eval/my_eval.py     # defines @task my_eval()
    src/my_eval/tasks.py       # accepted alternative
    src/my_eval/v8.py          # defines the tasks: warned, not failed
    ```
    """
    candidates = main_file_candidates(ctx.path, ctx.name)
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            ast.parse(candidate.read_text(encoding="utf-8"))
        except SyntaxError as e:
            yield Diagnostic(
                f"Syntax error in {candidate.name}: {e}", file=candidate, line=e.lineno
            )
            return

    modules = task_modules(ctx)
    expected = " or ".join(c.name for c in candidates)
    if not modules:
        present = [c for c in candidates if c.exists()]
        if present:
            message = f"{present[0].name} has no @task decorated functions, and no other module defines one"
        else:
            message = f"No module defines a @task function; expected {expected}"
        yield Diagnostic(message, file=present[0] if present else candidates[0])
        return

    if modules[0] in candidates:
        task_functions = _find_task_functions(modules[0])
        yield Outcome(
            "pass",
            f"{modules[0].name} has {len(task_functions)} @task function(s): {task_functions}",
        )
        return

    for module in modules:
        yield Diagnostic(
            f"@task functions are defined in {module.relative_to(ctx.path).as_posix()} rather than {expected}",
            file=module,
            severity="warning",
            hint=f"move them to {expected}, the modules readers look in first",
        )


@rule(
    code="IEFS003",
    name="init_exports",
    category="file_structure",
    summary="__init__.py exports every @task function in the package",
    references=(
        inspect_docs(
            "extensions-components", "Extensions: Components: Registration", "registration"
        ),
    ),
)
def init_exports(ctx: LintContext) -> Iterable[Finding]:
    """``__init__.py`` exports every ``@task`` function in the package.

    ## What it does
    Reads the task functions from every module that defines one and checks each
    name appears in ``__init__.py``, either in ``__all__`` or imported with
    ``from ... import``. One diagnostic per missing task, with the import to add.

    ## Why is this bad?
    ``inspect eval my_eval/task`` resolves tasks through the package, so a task the
    package does not export is a task nobody can run by name.

    ## Example
    ```python
    # __init__.py
    from .my_eval import my_eval, my_eval_hard

    __all__ = ["my_eval", "my_eval_hard"]
    ```
    """
    init_file = ctx.path / "__init__.py"

    if not init_file.exists():
        yield Diagnostic("Missing __init__.py file", file=init_file)
        return

    try:
        ast.parse(init_file.read_text(encoding="utf-8"))
    except SyntaxError as e:
        yield Diagnostic(f"Syntax error in __init__.py: {e}", file=init_file, line=e.lineno)
        return

    tasks = [(module, fn) for module in task_modules(ctx) for fn in _find_task_functions(module)]
    if not tasks:
        yield Outcome("skip", "No task functions to check for exports")
        return

    exported_names = _get_exported_names(init_file)
    missing = [(module, fn) for module, fn in tasks if fn not in exported_names]
    for module, fn in missing:
        yield Diagnostic(
            f"__init__.py does not export the @task function {fn!r}",
            file=init_file,
            hint=f"add `from {_relative_module(ctx, module)} import {fn}` and list it in __all__",
        )
    if not missing:
        yield Outcome("pass", f"__init__.py exports all {len(tasks)} task function(s)")


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
    references=(
        inspect_docs(
            "extensions-components", "Extensions: Components: Registration", "registration"
        ),
        inspect_docs("extensions-components", "Extensions: Components: Tasks", "tasks"),
    ),
)
def registry(ctx: LintContext) -> Iterable[Finding]:
    """The evaluation is registered so ``inspect eval`` can find its tasks.

    ## What it does
    With ``registry = "entry-points"`` (the template layout) the package or its
    module must appear under ``[project.entry-points.inspect_ai]`` in
    ``pyproject.toml``. With ``registry = "module"`` (the inspect_evals monorepo)
    the registry module must import it. ``registry = "none"`` skips the rule.

    ## Why is this bad?
    An unregistered evaluation runs from a file path in development and then cannot
    be found by name anywhere else.

    ## Example
    ```toml
    [project.entry-points.inspect_ai]
    my_eval = "my_eval"
    ```

    ## Options
    - `registry`
    - `registry-module`
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
    """``eval.yaml`` exists, is a mapping, and defines the required fields.

    ## What it does
    Parses ``eval.yaml`` in the package and reports one diagnostic per required
    field that is missing, or one for a file that is not valid YAML or not a
    mapping. With ``eval-yaml-required = false`` a missing file is a skip, for
    repositories whose metadata lives in the inspect_evals register; a present
    file is still validated.

    ## Why is this bad?
    ``eval.yaml`` is what listings, the register and the README generator read.
    A missing field there is a missing field everywhere downstream.

    ## Example
    ```yaml
    title: GPQA
    description: Graduate-level science questions.
    group: Knowledge
    contributors: [someone]
    tasks:
      - name: gpqa_diamond
    ```

    ## Options
    - `eval-yaml-required`
    - `eval-yaml-required-fields`
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
    """``README.md`` exists and has no ``TODO:`` markers.

    ## What it does
    Checks the package has a ``README.md``; with ``readme-location = "repo-root"``
    the repository's top-level README is accepted when the package has none. Warns
    once per line that still contains ``TODO:``.

    ## Why is this bad?
    The README is the evaluation's front door. A missing one leaves users guessing
    at what the evaluation measures; a leftover ``TODO:`` is a section the author
    meant to write.

    ## Options
    - `readme-location`
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
