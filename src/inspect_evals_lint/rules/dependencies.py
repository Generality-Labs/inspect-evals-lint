"""Dependency check: every third-party import must be declared in a pyproject dependency group."""

from __future__ import annotations

import ast
import functools
import re
import sys
import tomllib
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, packages_distributions, requires
from pathlib import Path
from typing import Any, cast

from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.context import LintContext, is_package
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import iter_python_files


def _normalize_name(name: str) -> str:
    """Canonical PEP 503 form: lower-cased, with runs of ``-``, ``_`` and ``.`` collapsed to ``-``.

    ``inspect_ai`` and ``inspect-ai`` name the same distribution, and distribution
    metadata uses whichever spelling the author wrote, so every name is compared
    in this form.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _extract_package_name(dep: str) -> str | None:
    """Normalised package name from a requirement string, stripping specifiers, extras and markers."""
    dep_name = re.split(r"[<>=!~@,;[\s]", dep)[0].strip()
    return _normalize_name(dep_name) if dep_name else None


@functools.cache
def _get_stdlib_modules() -> frozenset[str]:
    """Standard-library module names plus ``typing_extensions``, the ubiquitous backport."""
    return frozenset(sys.stdlib_module_names | {"typing_extensions"})


@functools.cache
def _load_toml_at(path: Path, mtime_ns: int) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file, caching by path and modification time so edits are seen."""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    return _load_toml_at(path, mtime_ns)


def _names(deps: object) -> set[str]:
    if not isinstance(deps, list):
        return set()
    items = cast(list[object], deps)
    return {name for dep in items if isinstance(dep, str) and (name := _extract_package_name(dep))}


def _declared_core_dependencies(repo_root: Path) -> frozenset[str]:
    """``[project].dependencies`` of the root pyproject, normalised."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return frozenset()
    data = _load_toml(pyproject_path)
    return frozenset(_names(data.get("project", {}).get("dependencies", [])))


def _installed_requirements(dist: str) -> frozenset[str]:
    """Normalised names of what the installed distribution ``dist`` requires unconditionally.

    Requirements guarded by an ``extra ==`` marker are left out: nobody asked for
    that extra. Empty when ``dist`` is not installed, so the closure below only
    grows inside the project's own environment.
    """
    try:
        listed = requires(dist) or []
    except PackageNotFoundError:
        return frozenset()
    names: set[str] = set()
    for requirement in listed:
        spec, _, marker = requirement.partition(";")
        if "extra" in marker:
            continue
        name = _extract_package_name(spec)
        if name:
            names.add(name)
    return frozenset(names)


def _get_core_dependencies(repo_root: Path) -> frozenset[str]:
    """``[project].dependencies`` and, for those installed, everything they require in turn.

    A transitive requirement of a core dependency is installed by construction,
    so importing it needs no declaration of its own: ``inspect_ai`` returns
    pydantic models from its public API, and every evaluation that types one
    imports ``pydantic``.
    """
    closure: set[str] = set()
    pending = list(_declared_core_dependencies(repo_root))
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        pending.extend(_installed_requirements(name))
    return frozenset(closure)


# Import name -> distribution name for packages that may not be installed.
# importlib.metadata only knows about installed distributions, so common
# mismatches get a static fallback.
_KNOWN_IMPORT_ALIASES: dict[str, str] = {
    "sklearn": "scikit-learn",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "odf": "odfpy",
}


@functools.cache
def _get_import_to_package_map() -> dict[str, str]:
    """Import name -> distribution name, from installed packages plus static aliases."""
    mapping = {imp: _normalize_name(dist) for imp, dist in _KNOWN_IMPORT_ALIASES.items()}
    for import_name, distributions in packages_distributions().items():
        if distributions:
            mapping[import_name] = _normalize_name(distributions[0])
    return mapping


Site = tuple[int, int | None, int | None]
"""Line, column and end line (None when single-line) of an import statement."""


class _ImportVisitor(ast.NodeVisitor):
    """Collect top-level module names, split by whether the import runs when the module loads.

    An import is *eager* when it sits at module scope outside any ``try`` block
    and any ``if TYPE_CHECKING:`` block. Imports inside a function body, a
    ``try`` statement or a type-checking block are *lazy*: they run later,
    conditionally, or never. The first site of each name is kept.
    """

    def __init__(self) -> None:
        self.eager: dict[str, Site] = {}
        self.lazy: dict[str, Site] = {}
        self._depth = 0

    def _record(self, name: str, node: ast.AST) -> None:
        target = self.lazy if self._depth else self.eager
        top = name.split(".")[0]
        if top not in target:
            target[top] = (getattr(node, "lineno", 1), _column(node), _end_line(node))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._record(node.module, node)

    def _visit_guarded(self, node: ast.AST) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = _visit_guarded  # noqa: N815
    visit_AsyncFunctionDef = _visit_guarded  # noqa: N815
    visit_Lambda = _visit_guarded  # noqa: N815
    visit_Try = _visit_guarded  # noqa: N815
    visit_TryStar = _visit_guarded  # noqa: N815

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            self._visit_guarded(node)
        else:
            self.generic_visit(node)


def _column(node: ast.AST) -> int | None:
    offset = getattr(node, "col_offset", None)
    return offset + 1 if isinstance(offset, int) else None


def _end_line(node: ast.AST) -> int | None:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    return end if isinstance(start, int) and isinstance(end, int) and end > start else None


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _get_imports_from_file(
    file_path: Path,
) -> tuple[dict[str, Site], dict[str, Site], str | None]:
    """``(eager, lazy)`` top-level module names with their first site, plus a syntax-error message if any."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return {}, {}, str(e)
    visitor = _ImportVisitor()
    visitor.visit(tree)
    return visitor.eager, visitor.lazy, None


Location = tuple[Path, int, int | None, int | None]


def _get_all_imports_from_package(
    ctx: LintContext,
) -> tuple[dict[str, Location], dict[str, Location], list[Diagnostic]]:
    """``(eager, lazy, parse diagnostics)`` across the package; a name eager anywhere counts as eager."""
    eager: dict[str, Location] = {}
    lazy: dict[str, Location] = {}
    failures: list[Diagnostic] = []
    for py_file in iter_python_files(ctx):
        file_eager, file_lazy, error = _get_imports_from_file(py_file)
        if error:
            failures.append(Diagnostic(f"Could not parse file: {error}", file=py_file, line=1))
            continue
        for name, (line, column, end_line) in file_eager.items():
            eager.setdefault(name, (py_file, line, column, end_line))
        for name, (line, column, end_line) in file_lazy.items():
            lazy.setdefault(name, (py_file, line, column, end_line))
    for name in eager:
        lazy.pop(name, None)
    return eager, lazy, failures


def _sibling_packages(repo_root: Path, config: LintConfig) -> set[str]:
    """Names of every package directly under ``source_root``: evaluations, helpers, ignored directories alike.

    ``from utils.metadata import load_version`` in a template repository imports the
    repository's own helper package, not a distribution, so it is never declared.
    """
    source_dir = config.source_dir(repo_root)
    if not source_dir.is_dir():
        return set()
    return {item.name for item in source_dir.iterdir() if is_package(item)}


def _get_local_modules(eval_path: Path) -> set[str]:
    """Module and package names defined inside the evaluation directory."""
    local_modules = {eval_path.name}
    for py_file in eval_path.rglob("*.py"):
        local_modules.add(py_file.stem)
        parent = py_file.parent
        while parent not in (eval_path, eval_path.parent):
            local_modules.add(parent.name)
            parent = parent.parent
    return local_modules


def _load_pyproject_optional_deps(repo_root: Path) -> dict[str, frozenset[str]]:
    """``[project.optional-dependencies]`` and ``[dependency-groups]`` (minus ``dev``) of the root pyproject."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return {}
    data = _load_toml(pyproject_path)

    optional_deps: dict[str, frozenset[str]] = {}
    for group_name, deps_list in data.get("project", {}).get("optional-dependencies", {}).items():
        optional_deps[str(group_name)] = frozenset(_names(deps_list))
    for group_name, deps_list in data.get("dependency-groups", {}).items():
        if group_name == "dev":
            continue
        optional_deps[str(group_name)] = optional_deps.get(
            str(group_name), frozenset()
        ) | frozenset(_names(deps_list))
    return optional_deps


def _load_isolated_package_deps(pyproject_path: Path) -> frozenset[str] | None:
    """Every dependency declared by an isolated eval package, or None if it has no pyproject."""
    if not pyproject_path.exists():
        return None
    data = _load_toml(pyproject_path)
    project = data.get("project", {})
    deps = _names(project.get("dependencies", []))
    for deps_list in project.get("optional-dependencies", {}).values():
        deps |= _names(deps_list)
    for group_name, deps_list in data.get("dependency-groups", {}).items():
        if group_name != "dev":
            deps |= _names(deps_list)
    return frozenset(deps)


def _external_imports(
    imports: dict[str, Location],
    package_path: Path,
    repo_root: Path,
    config: LintConfig,
) -> dict[str, Location]:
    """The imports that are neither standard library, core dependencies, the repo's own packages nor local."""
    local_modules = _get_local_modules(package_path) | _sibling_packages(repo_root, config)
    stdlib_modules = _get_stdlib_modules()
    core_deps = _get_core_dependencies(repo_root)
    import_to_package = _get_import_to_package_map()
    own_package = config.import_prefix.split(".")[0] if config.import_prefix else None

    external: dict[str, Location] = {}
    for imp, location in imports.items():
        imp_lower = imp.lower()
        package_name = import_to_package.get(imp, _normalize_name(imp))
        if (
            imp_lower not in stdlib_modules
            and package_name not in core_deps
            and imp != own_package
            and imp not in local_modules
            and imp_lower not in local_modules
        ):
            external[imp] = location
    return external


def _distribution(imp: str) -> str:
    return _get_import_to_package_map().get(imp, _normalize_name(imp))


def _undeclared(external: dict[str, Location], declared: set[str]) -> dict[str, Location]:
    """The external imports not covered by ``declared``."""
    return {
        imp: loc
        for imp, loc in external.items()
        if _distribution(imp) not in declared and _normalize_name(imp) not in declared
    }


def _all_declared_optional(repo_root: Path, config: LintConfig) -> set[str]:
    """Every name declared in some optional group, dependency group or isolated package.

    All isolated packages count, not one named after the package under review:
    a helper's deferred import is satisfied by whichever evaluation calls the
    function that performs it, and an isolated evaluation declares its
    dependencies only in its own ``pyproject.toml``.
    """
    declared: set[str] = set()
    for deps in _load_pyproject_optional_deps(repo_root).values():
        declared.update(deps)
    if config.isolated_packages_dir:
        for pyproject in sorted(
            (repo_root / config.isolated_packages_dir).glob("*/pyproject.toml")
        ):
            isolated = _load_isolated_package_deps(pyproject)
            if isolated is not None:
                declared.update(isolated)
    return declared


def _undeclared_diagnostics(
    undeclared: dict[str, Location], message: str, hint: str
) -> Iterable[Diagnostic]:
    for imp, (file, line, column, end_line) in sorted(undeclared.items(), key=lambda kv: kv[0]):
        yield Diagnostic(
            message.format(imp=imp, dist=_distribution(imp)),
            file=file,
            line=line,
            column=column,
            end_line=end_line,
            hint=hint,
        )


def _helper_dependencies(
    ctx: LintContext, eager: dict[str, Location], lazy: dict[str, Location]
) -> Iterable[Finding]:
    """The helper rule: module-level imports must be core dependencies; lazy ones need some declaration.

    A helper package is imported by every evaluation that uses it, so anything it
    imports at module load has to be installed everywhere, which only
    ``[project].dependencies`` guarantees. An import inside a function, a
    ``try`` block or an ``if TYPE_CHECKING:`` block is deferred or guarded and
    only needs to be declared somewhere.
    """
    external_eager = _external_imports(eager, ctx.path, ctx.root, ctx.config)
    if external_eager:
        yield from _undeclared_diagnostics(
            external_eager,
            "Module-level import of third-party package {imp!r} (package: {dist}) in a helper "
            "must be in [project].dependencies, because every evaluation that imports the helper loads it",
            "add it to [project].dependencies, or move the import inside the function that needs it",
        )
        return

    external_lazy = _external_imports(lazy, ctx.path, ctx.root, ctx.config)
    if not external_lazy:
        yield Outcome("pass", "No external dependencies detected beyond core requirements")
        return

    missing = _undeclared(external_lazy, _all_declared_optional(ctx.root, ctx.config))
    if missing:
        yield from _undeclared_diagnostics(
            missing,
            "Lazily imported package {imp!r} (package: {dist}) is not declared in any "
            "pyproject.toml optional-dependency group or isolated package",
            "declare it in the group of the evaluation that calls this code",
        )
    else:
        yield Outcome(
            "pass",
            f"Lazy external dependencies appear to be declared (imports: {len(external_lazy)})",
        )


@rule(
    code="IECQ004",
    name="external_dependencies",
    category="code_quality",
    scopes=("eval", "helper"),
    summary="Third-party imports are declared in pyproject.toml",
    references=(inspect_docs("tasks", "Tasks: Packaging", "packaging"),),
)
def external_dependencies(ctx: LintContext) -> Iterable[Finding]:
    """Third-party imports are declared in ``pyproject.toml``.

    ## What it does
    Collects every import in the package and treats one as external when it is not
    in the standard library, not in ``[project].dependencies`` or (when the
    linter runs in the project's environment) something those dependencies
    require in turn, not local to the package and not one of the repository's
    own packages (the ``import-prefix`` package, and every package under
    ``source-root`` such as a ``utils`` helper). For an evaluation, each external
    import must be declared in some ``[project.optional-dependencies]`` group or
    ``[dependency-groups]`` entry (other than ``dev``), or in the isolated
    package's ``pyproject.toml`` when ``isolated-packages-dir`` is set. With
    ``per-eval-dependency-group = true`` (the ``monorepo`` preset) the evaluation
    must also own a group named after itself unless it is isolated, so one
    evaluation's dependencies can be installed without the rest; a standalone
    repository declares its dependencies in ``[project].dependencies`` and any
    extra it likes.

    For a helper package the rule is different, because every evaluation that
    imports the helper loads whatever it imports at module level: those imports
    must be in ``[project].dependencies``, while imports inside a function, a
    ``try`` block or an ``if TYPE_CHECKING:`` block are deferred and only need to
    be declared in some group or in any isolated package. Helpers need no group of
    their own. One diagnostic per import, at its first site.

    Import-to-distribution mapping uses the packages installed in the current
    environment plus a few static aliases, so run the linter inside the project's
    environment. Names are compared in PEP 503 normalised form.

    ## Why is this bad?
    An import nobody declared works on the author's machine and fails for the next
    person with ``ModuleNotFoundError``, often only when a particular sample runs.

    ## Example
    A standalone repository:
    ```toml
    [project]
    dependencies = ["inspect_ai", "datasets>=4.0"]

    [project.optional-dependencies]
    modal = ["inspect_sandboxes"]
    ```
    The inspect_evals monorepo, with `per-eval-dependency-group = true`:
    ```toml
    [project.optional-dependencies]
    my_eval = ["datasets>=4.0", "scikit-learn"]
    ```

    ## Options
    - `per-eval-dependency-group`
    - `isolated-packages-dir`
    - `import-prefix`
    """
    eager, lazy, failures = _get_all_imports_from_package(ctx)
    yield from failures

    if ctx.kind == "helper":
        yield from _helper_dependencies(ctx, eager, lazy)
        return

    external = _external_imports({**lazy, **eager}, ctx.path, ctx.root, ctx.config)
    if not external:
        yield Outcome("pass", "No external dependencies detected beyond core requirements")
        return

    optional_deps = _load_pyproject_optional_deps(ctx.root)
    isolated_deps: frozenset[str] | None = None
    if ctx.config.isolated_packages_dir:
        isolated_deps = _load_isolated_package_deps(
            ctx.root / ctx.config.isolated_packages_dir / ctx.name / "pyproject.toml"
        )

    declared: set[str] = set()
    for deps in optional_deps.values():
        declared.update(deps)
    if isolated_deps is not None:
        declared.update(isolated_deps)

    missing = _undeclared(external, declared)
    if missing:
        yield from _undeclared_diagnostics(
            missing,
            "Import {imp!r} (package: {dist}) is not declared in pyproject.toml",
            f"add it to the [project.optional-dependencies] group named {ctx.name!r}"
            if ctx.config.per_eval_dependency_group
            else "add it to [project].dependencies, or to an optional-dependencies extra "
            "if only some configurations need it",
        )
    elif (
        ctx.config.per_eval_dependency_group
        and isolated_deps is None
        and ctx.name not in optional_deps
    ):
        yield Diagnostic(
            f"Evaluation uses external packages ({sorted(external)[:3]}) but has no dedicated "
            "optional-dependency group",
            file=ctx.root / "pyproject.toml",
            hint=f"add a [project.optional-dependencies] group named {ctx.name!r}",
        )
    else:
        yield Outcome(
            "pass", f"External dependencies appear to be declared (imports: {len(external)})"
        )
