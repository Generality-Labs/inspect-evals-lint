"""GitHub Actions workflow commands, one per finding, so a run annotates the pull request.

Only diagnostics are emitted: a failing finding as ``::error``, a warning as
``::warning``. Skips, passes and suppressed findings are not annotations. A
one-line summary follows so the job log still says what happened.
"""

from __future__ import annotations

from inspect_evals_lint.diagnostics import Diagnostic, RunReport
from inspect_evals_lint.render.paths import relative_to_root

_LEVEL = {"fail": "error", "warn": "warning"}


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
    message = (
        diagnostic.message
        if diagnostic.hint is None
        else f"{diagnostic.message}; {diagnostic.hint}"
    )
    return f"::{level} {','.join(properties)}::{_escape_message(message)}"


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
    return "\n".join(lines) + "\n"
