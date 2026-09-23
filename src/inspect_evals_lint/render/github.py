"""GitHub Actions workflow commands, one per finding, so a run annotates the pull request.

Only diagnostics are emitted: a failing finding as ``::error``, a warning as
``::warning``. Skips, passes and suppressed findings are not annotations. A
one-line summary follows so the job log still says what happened.

The message begins with the location and the rule (``src/x.py:3:7 IEBP003
sample_ids: ...``), the way ruff's GitHub format does, because the job log
shows only the message: the ``file=`` and ``line=`` properties go to the
annotations panel, which GitHub caps at ten errors and ten warnings per step.
"""

from __future__ import annotations

from inspect_evals_lint.diagnostics import Diagnostic, RunReport
from inspect_evals_lint.render.paths import relative_to_root

_LEVEL = {"fail": "error", "warn": "warning"}

ANNOTATION_CAP = 10
"""GitHub records at most this many annotations per level (error, warning) for one step."""


def _escape_property(text: str) -> str:
    return (
        text.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _escape_message(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(diagnostic: Diagnostic, run: RunReport) -> str | None:
    """The workflow command for one diagnostic, or None when it is not an error or warning."""
    level = _LEVEL.get(diagnostic.status)
    if level is None or diagnostic.rule is None:
        return None
    properties = [f"file={_escape_property(relative_to_root(diagnostic.file, run.root))}"]
    if diagnostic.line is not None:
        properties.append(f"line={diagnostic.line}")
        if diagnostic.column is not None:
            properties.append(f"col={diagnostic.column}")
    properties.append(f"title={_escape_property(f'{diagnostic.rule.code} {diagnostic.rule.name}')}")
    message = f"{location(diagnostic, run)} {diagnostic.rule.code} {diagnostic.rule.name}: {diagnostic.message}"
    if diagnostic.hint is not None:
        message += f"; {diagnostic.hint}"
    return f"::{level} {','.join(properties)}::{_escape_message(message)}"


def location(diagnostic: Diagnostic, run: RunReport) -> str:
    """``path``, ``path:line`` or ``path:line:col``, relative to the repository root."""
    text = relative_to_root(diagnostic.file, run.root)
    if diagnostic.line is not None:
        text += f":{diagnostic.line}"
        if diagnostic.column is not None:
            text += f":{diagnostic.column}"
    return text


def render_github(run: RunReport) -> str:
    """Every annotation, then a summary line, newline-terminated."""
    lines = [
        text
        for package in run.packages
        for d in package.diagnostics
        if (text := annotation(d, run)) is not None
    ]
    totals = run.summary()
    passed = sum(1 for p in run.packages if p.passed())
    lines.append(
        f"inspect-evals-lint: {passed}/{len(run.packages)} packages passed; "
        f"{totals['fail']} failed, {totals['warn']} warnings, {totals['suppressed']} suppressed"
    )
    if totals["fail"] > ANNOTATION_CAP or totals["warn"] > ANNOTATION_CAP:
        lines.append(
            f"GitHub shows at most {ANNOTATION_CAP} error and {ANNOTATION_CAP} warning annotations "
            "per step; every finding is listed above with its location, and in the job summary "
            "when GITHUB_STEP_SUMMARY is set."
        )
    return "\n".join(lines) + "\n"
