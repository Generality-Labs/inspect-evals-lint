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
from inspect_evals_lint.rules._compose import iter_compose_files

MARKER_PATTERN = re.compile(
    r"#\s*(?:(?P<legacy>noautolint(?:-file)?:)"
    r"|inspect-evals-lint:\s*ignore(?P<file>-file)?(?:\[(?P<selectors>[^\]]*)\])?)"
)
"""Any suppression marker: ``ignore[...]``, ``ignore-file[...]``, a malformed ``ignore``, or the removed ``noautolint``."""
REASON_START = re.compile(r"\s*--")
"""Where a comment's reason begins: ``ignore[IEBP003] -- ids are row numbers``. The reason runs to the end of the line and is never read for markers."""
PLACEHOLDER_PATTERN = re.compile(r"<[^<>]*>")

SUPPRESSION_RULE = "suppression_syntax"
"""The rule that reports on suppression comments; a comment cannot suppress its findings."""

MAX_FILE_HEADER_LINES = 10
"""A file-level suppression must appear within this many lines of the top."""

LEGACY_COMMENT_HINT = (
    "use `# inspect-evals-lint: ignore[<rule>] -- <reason>` on the line, or "
    "`# inspect-evals-lint: ignore-file[<rule>] -- <reason>` in the file's header"
)
LEGACY_FILE_HINT = (
    "list its rules under `per-file-ignores` in [tool.inspect-evals-lint] for this directory, "
    "or `exclude` the directory if it holds sandbox code"
)
MALFORMED_HINT = (
    "write ignore[<rule>] -- <reason> with a rule name, code or code prefix, "
    "e.g. ignore[IEFS006] -- <reason>"
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
    missing_reasons: list[SuppressionProblem] = field(default_factory=list)
    """Comments that suppress something but do not say why. Reported by ``suppression_syntax``."""
    comments: int = 0
    """How many well-formed ignore comments were read."""

    def covers(self, diagnostic: Diagnostic) -> bool:
        rule = diagnostic.rule
        if rule is None or rule.name == SUPPRESSION_RULE:
            # A comment cannot vouch for itself, so findings about suppression
            # comments are only suppressed by per-file-ignores.
            return False
        selectors = set(self.file_level.get(diagnostic.file, set()))
        if diagnostic.line is not None:
            # A formatter may move a trailing comment onto any line of a multi-line
            # statement, so every line the statement spans counts.
            lines = self.line_level.get(diagnostic.file, {})
            for line in range(diagnostic.line, (diagnostic.end_line or diagnostic.line) + 1):
                selectors |= lines.get(line, set())
        return any(selector_matches(s, rule) for s in selectors)


def _selectors(raw: str, path: Path, line: int, suppressions: Suppressions) -> list[str]:
    """The selectors in a bracketed list that name a rule, in written order; the others are recorded as problems."""
    selectors = list(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))
    if not selectors:
        suppressions.problems.append(
            SuppressionProblem(
                path,
                line,
                "an ignore comment must name at least one rule, so this one suppresses nothing",
                MALFORMED_HINT,
            )
        )
        return []
    known = [s for s in selectors if any(selector_matches(s, r) for r in rules())]
    for selector in sorted(set(selectors) - set(known)):
        suppressions.problems.append(
            SuppressionProblem(
                path,
                line,
                f"'{selector}' names no rule, so this selector suppresses nothing",
                SELECTOR_HINT,
            )
        )
    return known


def reason_of(rest: str) -> str | None:
    """The reason in the text after a marker's rule list, or None when there is none.

    A reason follows ``--``. Another tool's comment (``-- # noqa``), a second
    ``--`` and an unfilled ``<reason>`` placeholder are not reasons.
    """
    start = REASON_START.match(rest)
    if start is None:
        return None
    reason = rest[start.end() :].strip()
    if not reason or reason[0] in "#-" or PLACEHOLDER_PATTERN.fullmatch(reason):
        return None
    return reason


def _missing_reason(kind: str, known: list[str], path: Path, line: int) -> SuppressionProblem:
    written = f"{kind}[{', '.join(known)}]"
    return SuppressionProblem(
        path,
        line,
        f"{written} gives no reason",
        "say why the finding is acceptable after ' -- ', e.g. "
        f"`# inspect-evals-lint: {written} -- <reason>`",
    )


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
        position = 0
        while (match := MARKER_PATTERN.search(source_line, position)) is not None:
            rest = source_line[match.end() :]
            if match.group("legacy"):
                suppressions.problems.append(
                    SuppressionProblem(
                        path,
                        i,
                        "'# noautolint' comments are no longer read, so this one suppresses nothing",
                        LEGACY_COMMENT_HINT,
                    )
                )
                break
            kind = "ignore-file" if match.group("file") else "ignore"
            raw = match.group("selectors")
            if raw is None:
                suppressions.problems.append(
                    SuppressionProblem(
                        path,
                        i,
                        "an ignore comment must name at least one rule, so this one suppresses nothing",
                        MALFORMED_HINT,
                    )
                )
            elif kind == "ignore-file" and i > MAX_FILE_HEADER_LINES:
                suppressions.problems.append(
                    SuppressionProblem(
                        path,
                        i,
                        f"ignore-file must appear within the first {MAX_FILE_HEADER_LINES} lines, "
                        "so this one suppresses nothing",
                        "move it into the file's header, or use ignore[<rule>] -- <reason> "
                        "on the lines it covers",
                    )
                )
            else:
                suppressions.comments += 1
                known = _selectors(raw, path, i, suppressions)
                if known:
                    if kind == "ignore-file":
                        suppressions.file_level.setdefault(path, set()).update(known)
                    else:
                        target = _dockerfile_target(lines, index) if dockerfile else i
                        suppressions.line_level.setdefault(path, {}).setdefault(
                            target, set()
                        ).update(known)
                    if reason_of(rest) is None:
                        suppressions.missing_reasons.append(_missing_reason(kind, known, path, i))
            if REASON_START.match(rest):
                # The reason runs to the end of the line; markers quoted in it are prose.
                break
            position = match.end()


def load_suppressions(ctx: LintContext) -> Suppressions:
    """Collect ignore comments from the package's Python files, Dockerfiles and Compose files.

    Excluded Python files and Dockerfiles are never linted, so nothing in them can
    be suppressed and a stray comment there (in code shipped into a sandbox, say)
    is not read. Compose files are always linted, so their comments are always
    read. In a Dockerfile a comment on its own line applies to the instruction
    below it; in a Compose file a comment applies to its own line.

    Markers the linter does not read (the removed ``noautolint`` syntax, an
    ``ignore`` without a rule list, an ``ignore-file`` past the header, a selector
    naming no rule) are collected as ``problems``, and comments that apply without
    a reason as ``missing_reasons``, for the ``suppression_syntax`` rule to report;
    neither stops the package from being linted.
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
    for path in (*iter_python_files(ctx), *iter_dockerfiles(ctx), *iter_compose_files(ctx)):
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
