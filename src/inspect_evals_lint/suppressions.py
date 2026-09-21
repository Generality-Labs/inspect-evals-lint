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

from inspect_evals_lint.config import LintConfig, selector_matches
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.registry import rules
from inspect_evals_lint.rules._ast import (
    is_dockerfile,
    iter_dockerfiles,
    iter_package_files,
    iter_python_files,
)

LINE_PATTERN = re.compile(r"#\s*inspect-evals-lint:\s*ignore\[([^\]]*)\]")
FILE_PATTERN = re.compile(r"#\s*inspect-evals-lint:\s*ignore-file\[([^\]]*)\]")
MALFORMED_PATTERN = re.compile(r"#\s*inspect-evals-lint:\s*ignore(?:-file)?(?!\[)")
LEGACY_PATTERN = re.compile(r"#\s*noautolint(?:-file)?:")

MAX_FILE_HEADER_LINES = 10
"""A file-level suppression must appear within this many lines of the top."""

LEGACY_COMMENT_HINT = (
    "use `# inspect-evals-lint: ignore[<rule>]` on the line, or "
    "`# inspect-evals-lint: ignore-file[<rule>]` in the file's header"
)
LEGACY_FILE_HINT = (
    "list its rules under `per-file-ignores` in [tool.inspect-evals-lint] for this directory, "
    "or `exclude` the directory if it holds sandbox code"
)
SELECTOR_HINT = (
    "use a rule name, a code such as IEFS006, or a code prefix such as IEFS "
    "(`--list-rules` shows them)"
)


@dataclass
class SuppressionProblem:
    """A marker the linter does not read, so it suppresses nothing. Reported by ``suppression_syntax``."""

    file: Path
    line: int | None
    message: str
    hint: str


@dataclass
class Suppressions:
    """Suppressions collected from comments under one package."""

    file_level: dict[Path, set[str]] = field(default_factory=dict)
    line_level: dict[Path, dict[int, set[str]]] = field(default_factory=dict)
    problems: list[SuppressionProblem] = field(default_factory=list)
    comments: int = 0
    """How many well-formed ignore comments were read."""

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


def _selectors(raw: str, path: Path, line: int, suppressions: Suppressions) -> set[str]:
    """The selectors in a bracketed list that name a rule; the others are recorded as problems."""
    selectors = {s.strip() for s in raw.split(",") if s.strip()}
    if not selectors:
        suppressions.problems.append(
            SuppressionProblem(
                path,
                line,
                "an ignore comment must name at least one rule, so this one suppresses nothing",
                "write ignore[<rule>] with a rule name, code or code prefix, e.g. ignore[IEFS006]",
            )
        )
        return set()
    known = {s for s in selectors if any(selector_matches(s, r) for r in rules())}
    for selector in sorted(selectors - known):
        suppressions.problems.append(
            SuppressionProblem(
                path,
                line,
                f"'{selector}' names no rule, so this selector suppresses nothing",
                SELECTOR_HINT,
            )
        )
    return known


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
        if LEGACY_PATTERN.search(source_line):
            suppressions.problems.append(
                SuppressionProblem(
                    path,
                    i,
                    "'# noautolint' comments are no longer read, so this one suppresses nothing",
                    LEGACY_COMMENT_HINT,
                )
            )
            continue
        file_match = FILE_PATTERN.search(source_line)
        if file_match:
            if i > MAX_FILE_HEADER_LINES:
                suppressions.problems.append(
                    SuppressionProblem(
                        path,
                        i,
                        f"ignore-file must appear within the first {MAX_FILE_HEADER_LINES} lines, "
                        "so this one suppresses nothing",
                        "move it into the file's header, or use ignore[<rule>] on the lines it covers",
                    )
                )
                continue
            suppressions.comments += 1
            known = _selectors(file_match.group(1), path, i, suppressions)
            if known:
                suppressions.file_level.setdefault(path, set()).update(known)
            continue
        line_match = LINE_PATTERN.search(source_line)
        if line_match:
            suppressions.comments += 1
            known = _selectors(line_match.group(1), path, i, suppressions)
            if known:
                target = _dockerfile_target(lines, index) if dockerfile else i
                suppressions.line_level.setdefault(path, {}).setdefault(target, set()).update(known)
            continue
        if MALFORMED_PATTERN.search(source_line):
            suppressions.problems.append(
                SuppressionProblem(
                    path,
                    i,
                    "an ignore comment must name at least one rule, so this one suppresses nothing",
                    "write ignore[<rule>] with a rule name, code or code prefix, e.g. ignore[IEFS006]",
                )
            )


def load_suppressions(ctx: LintContext) -> Suppressions:
    """Collect ignore comments from the package's Python files and Dockerfiles, skipping ``exclude``d ones.

    Excluded files are never linted, so nothing in them can be suppressed and a
    stray comment there (in code shipped into a sandbox, say) is not read. In a
    Dockerfile a comment on its own line applies to the instruction below it.

    Markers the linter does not read (the removed ``noautolint`` syntax, an
    ``ignore`` without a rule list, an ``ignore-file`` past the header, a selector
    naming no rule) are collected as ``problems`` for the ``suppression_syntax``
    rule to report; they never stop the package from being linted.
    """
    suppressions = Suppressions()
    for legacy_file in iter_package_files(ctx, ".noautolint"):
        suppressions.problems.append(
            SuppressionProblem(
                legacy_file,
                None,
                ".noautolint files are no longer read, so this one suppresses nothing",
                LEGACY_FILE_HINT,
            )
        )
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
