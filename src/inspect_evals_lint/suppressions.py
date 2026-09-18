"""Loading and applying ``noautolint`` suppressions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from inspect_evals_lint.diagnostics import Diagnostic

NOAUTOLINT_LINE_PATTERN = re.compile(r"#\s*noautolint:\s*([\w,\s]+)")
NOAUTOLINT_FILE_PATTERN = re.compile(r"#\s*noautolint-file:\s*([\w,\s]+)")

# A file-level suppression comment must appear within this many lines of the top.
MAX_FILE_HEADER_LINES = 10


@dataclass
class Suppressions:
    """Suppressions collected for one package, at package, directory, file and line level."""

    package_level: set[str] = field(default_factory=set)
    dir_level: dict[Path, set[str]] = field(default_factory=dict)
    file_level: dict[Path, set[str]] = field(default_factory=dict)
    line_level: dict[Path, dict[int, set[str]]] = field(default_factory=dict)

    def covers(self, names: set[str], file: Path, line: int | None) -> bool:
        """Whether any of ``names`` (a rule's name and code) is suppressed at the location."""
        if names & self.package_level:
            return True
        for directory, rules in self.dir_level.items():
            if file.is_relative_to(directory) and names & rules:
                return True
        if names & self.file_level.get(file, set()):
            return True
        return line is not None and bool(names & self.line_level.get(file, {}).get(line, set()))


def _parse_check_list(raw: str) -> set[str]:
    return {c.strip() for c in raw.split(",") if c.strip()}


def load_suppressions(package_path: Path) -> Suppressions:
    """Collect suppressions from ``.noautolint`` files and inline comments under ``package_path``."""
    suppressions = Suppressions()

    for noautolint_file in package_path.rglob(".noautolint"):
        checks: set[str] = set()
        for raw_line in noautolint_file.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#"):
                checks.add(stripped)

        if noautolint_file.parent == package_path:
            suppressions.package_level.update(checks)
        else:
            suppressions.dir_level[noautolint_file.parent] = checks

    for py_file in package_path.rglob("*.py"):
        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, source_line in enumerate(lines, start=1):
            if i <= MAX_FILE_HEADER_LINES:
                file_match = NOAUTOLINT_FILE_PATTERN.search(source_line)
                if file_match:
                    suppressions.file_level.setdefault(py_file, set()).update(
                        _parse_check_list(file_match.group(1))
                    )

            line_match = NOAUTOLINT_LINE_PATTERN.search(source_line)
            if line_match:
                suppressions.line_level.setdefault(py_file, {}).setdefault(i, set()).update(
                    _parse_check_list(line_match.group(1))
                )

    return suppressions


def apply_suppressions(diagnostics: list[Diagnostic], suppressions: Suppressions) -> None:
    """Mark diagnostics as suppressed where a suppression covers their site."""
    for diagnostic in diagnostics:
        names: set[str] = set()
        if diagnostic.rule is not None:
            names = {diagnostic.rule.name, diagnostic.rule.code}
        if suppressions.covers(names, diagnostic.file, diagnostic.line):
            diagnostic.suppressed = True
