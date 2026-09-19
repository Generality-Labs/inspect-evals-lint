"""Shared helpers for rules: AST parsing, decorator names, file iteration that honours ``exclude``."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic


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
        return ParsedFile(path=file_path, tree=ast.parse(file_path.read_text(encoding="utf-8")))
    except (SyntaxError, UnicodeDecodeError, OSError) as e:
        return ParseFailure(path=file_path, error=str(e))


def iter_package_files(
    ctx: LintContext, pattern: str, keep: Callable[[Path], bool] = Path.is_file
) -> list[Path]:
    """Files under the package matching ``pattern``, minus those the ``exclude`` globs rule out, in stable order."""
    files: list[Path] = []
    for path in sorted(ctx.path.rglob(pattern)):
        if not keep(path):
            continue
        relative = (
            path.relative_to(ctx.root).as_posix()
            if path.is_relative_to(ctx.root)
            else path.as_posix()
        )
        if not ctx.config.excludes(relative):
            files.append(path)
    return files


def iter_python_files(ctx: LintContext) -> list[Path]:
    """Python files under the package that ``exclude`` does not rule out."""
    return iter_package_files(ctx, "*.py")


def is_dockerfile(path: Path) -> bool:
    """``Dockerfile``, ``Dockerfile.gpu`` and the like; case-sensitive, so ``dockerfile.py`` is not one."""
    return path.is_file() and path.name.startswith("Dockerfile") and path.suffix != ".py"


def iter_dockerfiles(ctx: LintContext) -> list[Path]:
    """Dockerfiles under the package that ``exclude`` does not rule out."""
    return iter_package_files(ctx, "Dockerfile*", keep=is_dockerfile)


def parse_python_files(ctx: LintContext) -> ParseResults:
    """Parse every Python file under the package that ``exclude`` does not rule out."""
    results = ParseResults()
    for py_file in iter_python_files(ctx):
        outcome = safe_parse_file(py_file)
        if isinstance(outcome, ParsedFile):
            results.parsed.append(outcome)
        else:
            results.failed.append(outcome)
    return results


def parse_failures(results: ParseResults) -> list[Diagnostic]:
    """One diagnostic per file that could not be parsed."""
    return [
        Diagnostic(f"Could not parse file: {failure.error}", file=failure.path, line=1)
        for failure in results.failed
    ]


def end_line_of(node: ast.AST) -> int | None:
    """Last line of a node that spans more than one line; None for a single-line node."""
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    return end if isinstance(start, int) and isinstance(end, int) and end > start else None


def column_of(node: ast.AST) -> int | None:
    """1-based column of an AST node, when it has one."""
    offset = getattr(node, "col_offset", None)
    return offset + 1 if isinstance(offset, int) else None
