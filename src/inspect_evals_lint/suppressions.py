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
from inspect_evals_lint.rules._ast import is_dockerfile, iter_dockerfiles, iter_python_files

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
            # A formatter may move a trailing comment onto any line of a multi-line
            # statement, so every line the statement spans counts.
            lines = self.line_level.get(diagnostic.file, {})
            for line in range(diagnostic.line, (diagnostic.end_line or diagnostic.line) + 1):
                selectors |= lines.get(line, set())
        return any(selector_matches(s, rule) for s in selectors)


def _selectors(raw: str, where: str) -> set[str]:
    selectors = {s.strip() for s in raw.split(",") if s.strip()}
    if not selectors:
        raise ConfigError(
            f"{where}: an ignore comment must name at least one rule, e.g. ignore[IEFS006]"
        )
    return selectors


def _next_instruction_line(lines: list[str], index: int) -> int | None:
    """The 1-based number of the first instruction at or after ``index`` (0-based), if any."""
    for offset in range(index, len(lines)):
        stripped = lines[offset].strip()
        if stripped and not stripped.startswith("#"):
            return offset + 1
    return None


def _dockerfile_target(lines: list[str], index: int) -> int:
    """Where a comment in a Dockerfile applies.

    Dockerfile instructions take no trailing comment (``FROM x # c`` fails the
    build), so a comment on its own line covers the instruction that follows
    it; a comment inside a shell-form ``RUN`` stays on its own line.
    """
    if lines[index].strip().startswith("#"):
        return _next_instruction_line(lines, index + 1) or index + 1
    return index + 1


def _read_comments(path: Path, suppressions: Suppressions) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    dockerfile = is_dockerfile(path)

    for index, source_line in enumerate(lines):
        i = index + 1
        where = f"{path}:{i}"
        if LEGACY_PATTERN.search(source_line):
            raise ConfigError(f"{where}: {LEGACY_HELP}")
        file_match = FILE_PATTERN.search(source_line)
        if file_match:
            if i > MAX_FILE_HEADER_LINES:
                raise ConfigError(
                    f"{where}: ignore-file must appear within the first {MAX_FILE_HEADER_LINES} lines"
                )
            suppressions.file_level.setdefault(path, set()).update(
                _selectors(file_match.group(1), where)
            )
            continue
        line_match = LINE_PATTERN.search(source_line)
        if line_match:
            target = _dockerfile_target(lines, index) if dockerfile else i
            suppressions.line_level.setdefault(path, {}).setdefault(target, set()).update(
                _selectors(line_match.group(1), where)
            )
            continue
        if MALFORMED_PATTERN.search(source_line):
            raise ConfigError(
                f"{where}: an ignore comment must name at least one rule, e.g. ignore[IEFS006]"
            )


def load_suppressions(ctx: LintContext) -> Suppressions:
    """Collect ignore comments from the package's Python files and Dockerfiles, skipping ``exclude``d ones.

    Excluded files are never linted, so nothing in them can be suppressed and a
    stray comment there (in code shipped into a sandbox, say) is not an error.
    In a Dockerfile a comment on its own line applies to the instruction below it.

    Raises:
        ConfigError: a comment is malformed, or uses the removed ``noautolint`` syntax.
    """
    legacy_files = sorted(ctx.path.rglob(".noautolint"))
    if legacy_files:
        raise ConfigError(f"{legacy_files[0]}: .noautolint files are no longer read; {LEGACY_HELP}")

    suppressions = Suppressions()
    for path in (*iter_python_files(ctx), *iter_dockerfiles(ctx)):
        _read_comments(path, suppressions)
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
