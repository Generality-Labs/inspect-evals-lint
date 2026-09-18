"""Dependency check: every third-party import must be declared in a pyproject dependency group."""

from __future__ import annotations

import ast
import functools
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any, cast

from inspect_evals_lint.checks.utils import add_parse_errors_to_report, iter_python_files
from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.models import LintReport, LintResult, PackageKind


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


def _get_core_dependencies(repo_root: Path) -> frozenset[str]:
    """``[project].dependencies`` of the root pyproject."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return frozenset()
    data = _load_toml(pyproject_path)
    return frozenset(_names(data.get("project", {}).get("dependencies", [])))


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


class _ImportVisitor(ast.NodeVisitor):
    """Collect top-level module names, split by whether the import runs when the module loads.

    An import is *eager* when it sits at module scope outside any ``try`` block
    and any ``if TYPE_CHECKING:`` block. Imports inside a function body, a
    ``try`` statement or a type-checking block are *lazy*: they run later,
    conditionally, or never.
    """

    def __init__(self) -> None:
        self.eager: set[str] = set()
        self.lazy: set[str] = set()
        self._depth = 0

    def _record(self, name: str) -> None:
        (self.lazy if self._depth else self.eager).add(name.split(".")[0])

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._record(node.module)

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


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _get_imports_from_file(file_path: Path) -> tuple[set[str], set[str], str | None]:
    """``(eager, lazy)`` top-level module names imported by ``file_path``, plus a syntax-error message if any."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return set(), set(), str(e)
    visitor = _ImportVisitor()
    visitor.visit(tree)
    return visitor.eager, visitor.lazy, None


def _get_all_imports_from_eval(eval_path: Path) -> tuple[set[str], set[str], list[str]]:
    """``(eager, lazy, unparsable files)`` across the package; a name eager anywhere counts as eager."""
    eager: set[str] = set()
    lazy: set[str] = set()
    syntax_error_files: list[str] = []
    for py_file in iter_python_files(eval_path):
        file_eager, file_lazy, error = _get_imports_from_file(py_file)
        if error:
            syntax_error_files.append(str(py_file))
        else:
            eager.update(file_eager)
            lazy.update(file_lazy)
    return eager, lazy - eager, syntax_error_files


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
    imports: set[str],
    eval_path: Path,
    repo_root: Path,
    config: LintConfig,
) -> set[str]:
    """The imports that are neither standard library, core dependencies, the repo's own package nor local."""
    local_modules = _get_local_modules(eval_path)
    stdlib_modules = _get_stdlib_modules()
    core_deps = _get_core_dependencies(repo_root)
    import_to_package = _get_import_to_package_map()
    own_package = config.import_prefix.split(".")[0] if config.import_prefix else None

    external: set[str] = set()
    for imp in imports:
        imp_lower = imp.lower()
        package_name = import_to_package.get(imp, _normalize_name(imp))
        if (
            imp_lower not in stdlib_modules
            and package_name not in core_deps
            and imp != own_package
            and imp not in local_modules
            and imp_lower not in local_modules
        ):
            external.add(imp)
    return external


def _undeclared(external_imports: set[str], declared: set[str]) -> list[str]:
    """``"import (package: dist)"`` for each external import not covered by ``declared``."""
    import_to_package = _get_import_to_package_map()
    missing: list[str] = []
    for imp in sorted(external_imports):
        package_name = import_to_package.get(imp, _normalize_name(imp))
        if package_name not in declared and _normalize_name(imp) not in declared:
            missing.append(f"{imp} (package: {package_name})")
    return missing


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


def _check_helper_dependencies(
    repo_root: Path,
    eval_name: str,
    eval_path: Path,
    config: LintConfig,
    report: LintReport,
    eager: set[str],
    lazy: set[str],
) -> None:
    """The helper rule: module-level imports must be core dependencies; lazy ones need some group.

    A helper package is imported by every evaluation that uses it, so anything it
    imports at module load has to be installed everywhere, which only
    ``[project].dependencies`` guarantees. An import inside a function, a
    ``try`` block or an ``if TYPE_CHECKING:`` block is deferred or guarded and
    only needs to be declared somewhere.
    """
    external_eager = _external_imports(eager, eval_path, repo_root, config)
    if external_eager:
        report.add(
            LintResult(
                name="external_dependencies",
                status="fail",
                message=(
                    "Module-level third-party imports in a helper package must be in "
                    "[project].dependencies, because every evaluation that imports the "
                    f"helper loads them: {sorted(external_eager)[:5]}. Move the import inside "
                    "the function that needs it if only some evaluations do."
                ),
            )
        )
        return

    external_lazy = _external_imports(lazy, eval_path, repo_root, config)
    if not external_lazy:
        report.add(
            LintResult(
                name="external_dependencies",
                status="pass",
                message="No external dependencies detected beyond core requirements",
            )
        )
        return

    missing = _undeclared(external_lazy, _all_declared_optional(repo_root, config))
    if missing:
        report.add(
            LintResult(
                name="external_dependencies",
                status="fail",
                message=(
                    "Lazily imported packages must still be declared in some pyproject.toml "
                    f"optional-dependency group: {missing[:5]}"
                ),
            )
        )
    else:
        report.add(
            LintResult(
                name="external_dependencies",
                status="pass",
                message=(
                    f"Lazy external dependencies appear to be declared (imports: {len(external_lazy)})"
                ),
            )
        )


def check_external_dependencies(
    repo_root: Path,
    eval_name: str,
    eval_path: Path,
    config: LintConfig,
    report: LintReport,
    kind: PackageKind = "eval",
) -> None:
    """Check third-party imports are declared in an optional-dependency group (or isolated package).

    For ``kind="helper"`` the rule changes: see :func:`_check_helper_dependencies`.
    """
    eager, lazy, syntax_error_files = _get_all_imports_from_eval(eval_path)
    if add_parse_errors_to_report("external_dependencies", syntax_error_files, report):
        return

    if kind == "helper":
        _check_helper_dependencies(repo_root, eval_name, eval_path, config, report, eager, lazy)
        return

    external_imports = _external_imports(eager | lazy, eval_path, repo_root, config)

    if not external_imports:
        report.add(
            LintResult(
                name="external_dependencies",
                status="pass",
                message="No external dependencies detected beyond core requirements",
            )
        )
        return

    optional_deps = _load_pyproject_optional_deps(repo_root)

    isolated_deps: frozenset[str] | None = None
    if config.isolated_packages_dir:
        isolated_deps = _load_isolated_package_deps(
            repo_root / config.isolated_packages_dir / eval_name / "pyproject.toml"
        )
    is_isolated = isolated_deps is not None

    all_optional_deps: set[str] = set()
    for deps in optional_deps.values():
        all_optional_deps.update(deps)
    if isolated_deps is not None:
        all_optional_deps.update(isolated_deps)

    missing_deps = _undeclared(external_imports, all_optional_deps)

    if missing_deps:
        report.add(
            LintResult(
                name="external_dependencies",
                status="fail",
                message=f"External imports may need pyproject.toml optional-dependencies: {missing_deps[:5]}",
            )
        )
    elif not is_isolated and eval_name not in optional_deps:
        report.add(
            LintResult(
                name="external_dependencies",
                status="fail",
                message=f"Evaluation uses external packages ({list(external_imports)[:3]}...) but has no dedicated optional-dependency group",
            )
        )
    else:
        report.add(
            LintResult(
                name="external_dependencies",
                status="pass",
                message=f"External dependencies appear to be declared (imports: {len(external_imports)})",
            )
        )
