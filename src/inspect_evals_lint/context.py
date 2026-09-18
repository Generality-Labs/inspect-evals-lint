"""What a rule sees: the package under review, its layout, and the repository configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from inspect_evals_lint.config import LintConfig, load_config
from inspect_evals_lint.models import PackageKind


def is_package(path: Path) -> bool:
    """Whether ``path`` is a Python package: a directory with an ``__init__.py``."""
    return path.is_dir() and (path / "__init__.py").is_file()


def get_eval_path(repo_root: Path, name: str, config: LintConfig) -> Path | None:
    """The package directory, or None if it does not exist or is not a package."""
    path = config.eval_dir(repo_root, name)
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


def get_all_eval_names(repo_root: Path, config: LintConfig | None = None) -> list[str]:
    """Evaluation package names under ``config.source_root``.

    A directory counts when it has an ``__init__.py`` and is not hidden,
    underscore-prefixed, or listed in ``config.helper_dirs`` or ``config.ignore_dirs``.
    """
    config = config or load_config(repo_root)
    excluded = config.helper_dirs | config.ignore_dirs
    return [item.name for item in _candidate_dirs(repo_root, config) if item.name not in excluded]


def get_all_helper_names(repo_root: Path, config: LintConfig | None = None) -> list[str]:
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
            path=config.eval_dir(repo_root, name),
            kind=kind,
            config=config,
            test_path=test_path,
            test_search_path=test_path
            if kind == "eval"
            else (tests_root if tests_root.is_dir() else None),
        )
