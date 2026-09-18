"""Suppression comments and path-based ignores.

Comments are namespaced with the tool's name, the way pyright and mypy do it,
rather than ruff's ``# noqa``: ruff reads every ``# noqa`` comment and warns
about rule codes it does not know, so a shared spelling would turn every
suppression into a ruff warning in repositories that run both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from inspect_evals_lint.config import ConfigError, LintConfig, selector_matches
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.rules._ast import iter_python_files

LINE_PATTERN = re.compile(r"#\s*inspect-evals-lint:\s*ignore\[([^\]]*)\]")
FILE_PATTERN = re.compile(r"#\s*inspect-evals-lint:\s*ignore-file\[([^\]]*)\]")
MALFORMED_PATTERN = re.compile(r"#\s*inspect-evals-lint:\s*ignore(?:-file)?(?!\[)")
LEGACY_PATTERN = re.compile(r"#\s*noautolint(?:-file)?:")

MAX_FILE_HEADER_LINES = 10
"""A file-level suppression must appear within this many lines of the top."""

LEGACY_HELP = (
    "the noautolint syntax was removed; use `# inspect-evals-lint: ignore[<rule>]` on a line, "
    "`# inspect-evals-lint: ignore-file[<rule>]` in a file's header, or `per-file-ignores` "
    "in [tool.inspect-evals-lint] for whole directories"
)


@dataclass
class Suppressions:
    """Suppressions collected from comments under one package."""

    file_level: dict[Path, set[str]] = field(default_factory=dict)
    line_level: dict[Path, dict[int, set[str]]] = field(default_factory=dict)

    def covers(self, diagnostic: Diagnostic) -> bool:
        rule = diagnostic.rule
        if rule is None:
            return False
        selectors = set(self.file_level.get(diagnostic.file, set()))
        if diagnostic.line is not None:
            selectors |= self.line_level.get(diagnostic.file, {}).get(diagnostic.line, set())
        return any(selector_matches(s, rule) for s in selectors)


def _selectors(raw: str, where: str) -> set[str]:
    selectors = {s.strip() for s in raw.split(",") if s.strip()}
    if not selectors:
        raise ConfigError(
            f"{where}: an ignore comment must name at least one rule, e.g. ignore[IEFS006]"
        )
    return selectors


def load_suppressions(ctx: LintContext) -> Suppressions:
    """Collect ignore comments from the package's Python files, skipping ``exclude``d ones.

    Excluded files are never linted, so nothing in them can be suppressed and a
    stray comment there (in code shipped into a sandbox, say) is not an error.

    Raises:
        ConfigError: a comment is malformed, or uses the removed ``noautolint`` syntax.
    """
    legacy_files = sorted(ctx.path.rglob(".noautolint"))
    if legacy_files:
        raise ConfigError(f"{legacy_files[0]}: .noautolint files are no longer read; {LEGACY_HELP}")

    suppressions = Suppressions()
    for py_file in iter_python_files(ctx):
        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, source_line in enumerate(lines, start=1):
            where = f"{py_file}:{i}"
            if LEGACY_PATTERN.search(source_line):
                raise ConfigError(f"{where}: {LEGACY_HELP}")
            file_match = FILE_PATTERN.search(source_line)
            if file_match:
                if i > MAX_FILE_HEADER_LINES:
                    raise ConfigError(
                        f"{where}: ignore-file must appear within the first {MAX_FILE_HEADER_LINES} lines"
                    )
                suppressions.file_level.setdefault(py_file, set()).update(
                    _selectors(file_match.group(1), where)
                )
                continue
            line_match = LINE_PATTERN.search(source_line)
            if line_match:
                suppressions.line_level.setdefault(py_file, {}).setdefault(i, set()).update(
                    _selectors(line_match.group(1), where)
                )
                continue
            if MALFORMED_PATTERN.search(source_line):
                raise ConfigError(
                    f"{where}: an ignore comment must name at least one rule, e.g. ignore[IEFS006]"
                )

    return suppressions


def apply_suppressions(
    diagnostics: list[Diagnostic], suppressions: Suppressions, config: LintConfig, root: Path
) -> None:
    """Mark diagnostics as suppressed where a comment or a ``per-file-ignores`` pattern covers them."""
    for diagnostic in diagnostics:
        if diagnostic.rule is None:
            continue
        if suppressions.covers(diagnostic):
            diagnostic.suppressed = True
            continue
        file = diagnostic.file
        relative = (
            file.relative_to(root).as_posix()
            if file.is_absolute() and file.is_relative_to(root)
            else file.as_posix()
        )
        if config.ignored_in(relative, diagnostic.rule):
            diagnostic.suppressed = True
