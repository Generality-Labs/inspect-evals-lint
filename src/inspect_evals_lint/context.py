"""What a rule sees: the package under review, its layout, and the repository configuration."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from inspect_evals_lint.config import LintConfig, load_config
from inspect_evals_lint.diagnostics import PackageKind


class UnsupportedLayoutError(ValueError):
    """A task file the linter cannot treat as an evaluation package.

    Raised for a task file that does not exist, sits outside the repository, or
    has no ``__init__.py`` beside it (a bare module). The message says which, in
    the words the register lint service publishes.
    """


@dataclass(frozen=True)
class TaskLayout:
    """Where the package holding a task file sits, in configuration terms.

    The evaluation is the directory holding the task file; its parent is the
    ``source_root``; any enclosing packages between the source root and the
    repository root form the ``import_prefix``. ``india_evals/safeguards/task.py``
    lints ``safeguards`` under ``india_evals`` with prefix ``india_evals``.
    """

    eval_name: str
    source_root: str
    import_prefix: str

    def config(self, base: LintConfig) -> LintConfig:
        """``base`` with this layout's source root and import prefix."""
        return replace(base, source_root=self.source_root, import_prefix=self.import_prefix)


def task_layout(repo_root: Path, task_path: str) -> TaskLayout:
    """The layout of the package that holds ``task_path`` (repository-relative).

    Raises:
        UnsupportedLayoutError: the file is missing, escapes the repository, or is a bare module.
    """
    root = repo_root.resolve()
    task_file = (root / task_path).resolve()
    if not task_file.is_relative_to(root):
        raise UnsupportedLayoutError(f"task_path escapes the repository: {task_path}")
    if not task_file.is_file():
        raise UnsupportedLayoutError(f"task_path not found: {task_path}")
    eval_dir = task_file.parent
    if eval_dir == root or not is_package(eval_dir):
        raise UnsupportedLayoutError(
            f"{task_path} is not inside a package (no __init__.py next to it); "
            "inspect-evals-lint checks one package per evaluation"
        )
    source_root = eval_dir.parent
    prefix_parts: list[str] = []
    package = source_root
    while package != root and is_package(package):
        prefix_parts.insert(0, package.name)
        package = package.parent
    return TaskLayout(
        eval_name=eval_dir.name,
        source_root=source_root.relative_to(root).as_posix(),
        import_prefix=".".join(prefix_parts),
    )


def task_layouts(repo_root: Path, task_paths: Iterable[str]) -> list[TaskLayout]:
    """Distinct layouts for ``task_paths``; two task files in one package give one layout."""
    seen: dict[TaskLayout, None] = {}
    for task_path in task_paths:
        seen.setdefault(task_layout(repo_root, task_path))
    return list(seen)


def is_package(path: Path) -> bool:
    """Whether ``path`` is a Python package: a directory with an ``__init__.py``."""
    return path.is_dir() and (path / "__init__.py").is_file()


def package_path(repo_root: Path, name: str, config: LintConfig) -> Path | None:
    """The package directory, or None if it does not exist or is not a package."""
    path = config.package_dir(repo_root, name)
    return path if is_package(path) else None


def package_kind(name: str, config: LintConfig) -> PackageKind:
    """Whether ``name`` is linted as an evaluation or as a helper package."""
    return "helper" if name in config.helper_dirs else "eval"


def _candidate_dirs(repo_root: Path, config: LintConfig) -> list[Path]:
    source_dir = config.source_dir(repo_root)
    if not source_dir.is_dir():
        return []
    return sorted(
        item
        for item in source_dir.iterdir()
        if is_package(item) and not item.name.startswith(("_", "."))
    )


def evaluation_names(repo_root: Path, config: LintConfig | None = None) -> list[str]:
    """Evaluation package names under ``config.source_root``.

    A directory counts when it has an ``__init__.py`` and is not hidden,
    underscore-prefixed, or listed in ``config.helper_dirs`` or ``config.ignore_dirs``.
    """
    config = config or load_config(repo_root)
    excluded = config.helper_dirs | config.ignore_dirs
    return [item.name for item in _candidate_dirs(repo_root, config) if item.name not in excluded]


def helper_names(repo_root: Path, config: LintConfig | None = None) -> list[str]:
    """Helper package names under ``config.source_root``: entries of ``helper_dirs`` that are packages."""
    config = config or load_config(repo_root)
    return [
        item.name for item in _candidate_dirs(repo_root, config) if item.name in config.helper_dirs
    ]


def get_test_path(repo_root: Path, name: str, config: LintConfig) -> Path | None:
    """The package's test directory, or None if it does not exist.

    ``<tests_root>/<name>/`` when present; with the ``flat`` layout, ``tests_root``
    itself when it holds test files directly.
    """
    per_package = config.tests_dir(repo_root) / name
    if per_package.is_dir():
        return per_package
    tests_root = config.tests_dir(repo_root)
    if config.tests_layout == "flat" and has_test_files(tests_root):
        return tests_root
    return None


def has_test_files(directory: Path) -> bool:
    return directory.is_dir() and any(
        path.is_file() for path in (*directory.glob("test_*.py"), *directory.glob("*_test.py"))
    )


@dataclass(frozen=True)
class LintContext:
    """Everything a rule needs about the package under review.

    ``path`` is where the package is expected to live, whether or not it exists;
    the location rule is the one that reports on that. Every other rule only runs
    once ``path`` is known to be a package.
    """

    root: Path
    name: str
    path: Path
    kind: PackageKind
    config: LintConfig
    test_path: Path | None
    """``<tests_root>/<name>/`` (or the flat tests root), when it exists."""
    test_search_path: Path | None
    """Where tests for custom components are looked for: ``test_path`` for an evaluation, the whole tests root for a helper."""

    @property
    def tests_root(self) -> Path:
        return self.config.tests_dir(self.root)

    @classmethod
    def build(cls, repo_root: Path, name: str, config: LintConfig) -> LintContext:
        kind = package_kind(name, config)
        test_path = get_test_path(repo_root, name, config)
        tests_root = config.tests_dir(repo_root)
        return cls(
            root=repo_root,
            name=name,
            path=config.package_dir(repo_root, name),
            kind=kind,
            config=config,
            test_path=test_path,
            test_search_path=test_path
            if kind == "eval"
            else (tests_root if tests_root.is_dir() else None),
        )
