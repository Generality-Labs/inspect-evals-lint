"""Shared helpers for checks: AST parsing, decorator names, ``.noautolint`` directory skipping."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from inspect_evals_lint.models import LintReport, LintResult


@dataclass
class Issue:
    """A location a check has flagged."""

    file: str
    line: int
    detail: str = ""

    def __str__(self) -> str:
        if self.detail:
            return f"{self.file}:{self.line} ({self.detail})"
        return f"{self.file}:{self.line}"


@dataclass
class ParsedFile:
    path: Path
    tree: ast.AST


@dataclass
class ParseFailure:
    path: Path
    error: str


@dataclass
class ParseResults:
    """Outcome of parsing a set of Python files."""

    parsed: list[ParsedFile] = field(default_factory=list)
    failed: list[ParseFailure] = field(default_factory=list)

    @property
    def failed_paths(self) -> list[str]:
        return [str(f.path) for f in self.failed]


def get_decorator_name(decorator: ast.expr) -> str | None:
    """Return the bare name of a decorator: ``@x``, ``@x()``, ``@m.x`` and ``@m.x()`` all give ``x``."""
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Call):
        if isinstance(decorator.func, ast.Name):
            return decorator.func.id
        if isinstance(decorator.func, ast.Attribute):
            return decorator.func.attr
    elif isinstance(decorator, ast.Attribute):
        return decorator.attr
    return None


def get_call_name(node: ast.Call) -> str | None:
    """Return the callee name of ``foo()`` or ``obj.foo()``; None for subscripts and chained calls."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def safe_parse_file(file_path: Path) -> ParsedFile | ParseFailure:
    try:
        return ParsedFile(path=file_path, tree=ast.parse(file_path.read_text()))
    except (SyntaxError, UnicodeDecodeError, OSError) as e:
        return ParseFailure(path=file_path, error=str(e))


def noautolint_skip_dirs(eval_path: Path) -> set[Path]:
    """Sub-directories of ``eval_path`` holding a ``.noautolint`` file.

    Files under these directories are excluded from AST-based checks entirely.
    The eval directory's own ``.noautolint`` is not included: it lists per-check
    suppressions rather than opting the whole tree out.
    """
    return {f.parent for f in eval_path.rglob(".noautolint") if f.parent != eval_path}


def is_path_under_any(path: Path, skip_dirs: set[Path]) -> bool:
    return any(path.is_relative_to(skip_dir) for skip_dir in skip_dirs)


def iter_python_files(eval_path: Path) -> list[Path]:
    """Python files under ``eval_path``, skipping ``.noautolint`` sub-directories, in stable order."""
    skip_dirs = noautolint_skip_dirs(eval_path)
    return [
        p for p in eval_path.rglob("*.py") if not (skip_dirs and is_path_under_any(p, skip_dirs))
    ]


def parse_python_files(eval_path: Path) -> ParseResults:
    """Parse every Python file under ``eval_path`` (honouring ``.noautolint`` sub-directories)."""
    results = ParseResults()
    for py_file in iter_python_files(eval_path):
        outcome = safe_parse_file(py_file)
        if isinstance(outcome, ParsedFile):
            results.parsed.append(outcome)
        else:
            results.failed.append(outcome)
    return results


def add_parse_errors_to_report(
    check_name: str, failed_paths: list[str], report: LintReport
) -> bool:
    """Record a failure for unparsable files; return True when the caller should stop."""
    if not failed_paths:
        return False
    report.add(
        LintResult(
            name=check_name,
            status="fail",
            message=f"Could not parse {len(failed_paths)} file(s): {failed_paths}",
        )
    )
    return True
