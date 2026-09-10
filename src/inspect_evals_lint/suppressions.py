"""Loading and applying ``noautolint`` suppressions."""

from __future__ import annotations

import re
from pathlib import Path

from inspect_evals_lint.models import LintResult, Suppressions

NOAUTOLINT_LINE_PATTERN = re.compile(r"#\s*noautolint:\s*([\w,\s]+)")
NOAUTOLINT_FILE_PATTERN = re.compile(r"#\s*noautolint-file:\s*([\w,\s]+)")

# A file-level suppression comment must appear within this many lines of the top.
MAX_FILE_HEADER_LINES = 10


def _parse_check_list(raw: str) -> set[str]:
    return {c.strip() for c in raw.split(",") if c.strip()}


def load_suppressions(eval_path: Path) -> Suppressions:
    """Collect suppressions from ``.noautolint`` files and inline comments under ``eval_path``."""
    suppressions = Suppressions()

    for noautolint_file in eval_path.rglob(".noautolint"):
        checks: set[str] = set()
        for raw_line in noautolint_file.read_text().splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#"):
                checks.add(stripped)

        if noautolint_file.parent == eval_path:
            suppressions.eval_level.update(checks)
        else:
            suppressions.dir_level[str(noautolint_file.parent)] = checks

    for py_file in eval_path.rglob("*.py"):
        try:
            lines = py_file.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        file_key = str(py_file)
        for i, source_line in enumerate(lines, start=1):
            if i <= MAX_FILE_HEADER_LINES:
                file_match = NOAUTOLINT_FILE_PATTERN.search(source_line)
                if file_match:
                    suppressions.file_level.setdefault(file_key, set()).update(
                        _parse_check_list(file_match.group(1))
                    )

            line_match = NOAUTOLINT_LINE_PATTERN.search(source_line)
            if line_match:
                suppressions.line_level.setdefault(file_key, {}).setdefault(i, set()).update(
                    _parse_check_list(line_match.group(1))
                )

    return suppressions


def apply_suppressions(results: list[LintResult], suppressions: Suppressions) -> None:
    """Rewrite ``fail``/``warn`` results to ``suppressed`` where a suppression applies."""
    for result in results:
        if result.status in ("fail", "warn") and suppressions.is_suppressed(
            result.name, result.file, result.line
        ):
            result.status = "suppressed"
            result.message = f"[suppressed] {result.message}"
