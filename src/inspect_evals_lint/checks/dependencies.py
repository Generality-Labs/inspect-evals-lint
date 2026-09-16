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
from inspect_evals_lint.models import LintReport, LintResult


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
        return tomllib.loads(path.read_text())
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


def _get_imports_from_file(file_path: Path) -> tuple[set[str], str | None]:
    """Top-level module names imported by ``file_path``, plus a syntax-error message if any."""
    try:
        tree = ast.parse(file_path.read_text())
    except SyntaxError as e:
        return set(), str(e)

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports, None


def _get_all_imports_from_eval(eval_path: Path) -> tuple[set[str], list[str]]:
    all_imports: set[str] = set()
    syntax_error_files: list[str] = []
    for py_file in iter_python_files(eval_path):
        imports, error = _get_imports_from_file(py_file)
        if error:
            syntax_error_files.append(str(py_file))
        else:
            all_imports.update(imports)
    return all_imports, syntax_error_files


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


def check_external_dependencies(
    repo_root: Path,
    eval_name: str,
    eval_path: Path,
    config: LintConfig,
    report: LintReport,
) -> None:
    """Check third-party imports are declared in an optional-dependency group (or isolated package)."""
    imports, syntax_error_files = _get_all_imports_from_eval(eval_path)
    if add_parse_errors_to_report("external_dependencies", syntax_error_files, report):
        return

    local_modules = _get_local_modules(eval_path)
    stdlib_modules = _get_stdlib_modules()
    core_deps = _get_core_dependencies(repo_root)
    import_to_package = _get_import_to_package_map()
    own_package = config.import_prefix.split(".")[0] if config.import_prefix else None

    external_imports: set[str] = set()
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
            external_imports.add(imp)

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

    missing_deps: list[str] = []
    for imp in sorted(external_imports):
        package_name = import_to_package.get(imp, _normalize_name(imp))
        if package_name not in all_optional_deps and _normalize_name(imp) not in all_optional_deps:
            missing_deps.append(f"{imp} (package: {package_name})")

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
