"""The JSON document: one run as a versioned, machine-readable record. Shape in docs/output.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspect_evals_lint import __version__
from inspect_evals_lint.diagnostics import Diagnostic, Outcome, PackageReport, RunReport
from inspect_evals_lint.render.paths import relative_to_root

SCHEMA_VERSION = 1
"""Bumped when the document changes shape. Consumers should check it before reading anything else."""


def _outcome_to_dict(outcome: Outcome) -> dict[str, Any]:
    return {
        "rule": outcome.rule.name if outcome.rule else None,
        "code": outcome.rule.code if outcome.rule else None,
        "category": outcome.rule.category if outcome.rule else None,
        "status": outcome.status,
        "message": outcome.message,
    }


def _diagnostic_to_dict(diagnostic: Diagnostic, root: Path | None) -> dict[str, Any]:
    return {
        "rule": diagnostic.rule.name if diagnostic.rule else None,
        "code": diagnostic.rule.code if diagnostic.rule else None,
        "category": diagnostic.rule.category if diagnostic.rule else None,
        "severity": diagnostic.severity,
        "status": diagnostic.status,
        "message": diagnostic.message,
        "file": relative_to_root(diagnostic.file, root),
        "line": diagnostic.line,
        "column": diagnostic.column,
        "hint": diagnostic.hint,
    }


def package_to_dict(report: PackageReport, root: Path | None = None) -> dict[str, Any]:
    """One package's report as a JSON-serialisable mapping."""
    return {
        "name": report.name,
        "kind": report.kind,
        "passed": report.passed(),
        "skipped": report.skipped,
        "summary": report.summary(),
        "outcomes": [_outcome_to_dict(o) for o in report.outcomes],
        "diagnostics": [_diagnostic_to_dict(d, root) for d in report.diagnostics],
    }


def run_to_dict(run: RunReport) -> dict[str, Any]:
    """The whole run as a JSON-serialisable mapping.

    ``passed`` mirrors the CLI exit code: true when no rule failed in any package.
    File paths are relative to the root when they fall under it, so the document
    does not depend on the machine that produced it.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "version": __version__,
        "root": str(run.root),
        "passed": run.passed(),
        "summary": run.summary(),
        "packages": [package_to_dict(p, run.root) for p in run.packages],
    }


def render_json(run: RunReport) -> str:
    """The ``--output-format json`` document: :func:`run_to_dict` as indented JSON with a trailing newline."""
    return json.dumps(run_to_dict(run), indent=2) + "\n"
