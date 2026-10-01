"""Ruff configuration check: the configuration governing a package enables a curated set of ruff rules."""

from __future__ import annotations

import functools
import inspect
import json
import shutil
import subprocess
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from inspect_evals_lint.config import ConfigError
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import RuleFn, rule

RULE_NAME = "ruff_rules_enabled"
OPTION_TABLE = f"[tool.inspect-evals-lint.{RULE_NAME}]"


@dataclass(frozen=True)
class CuratedRule:
    """A ruff rule every evaluation's ruff configuration should enable, with the evidence for it."""

    code: str
    name: str
    why: str
    evidence: str
    """Markdown: the pull request or incident that motivated the entry."""
    probe: str
    """Source the rule reports. Linted as though it were a file in the package, to ask ruff whether the rule is on there."""


CURATED_RULES: tuple[CuratedRule, ...] = (
    CuratedRule(
        code="PLW1514",
        name="unspecified-encoding",
        why=(
            "``open()`` and ``Path.read_text()`` without ``encoding=`` decode with the host's "
            "locale, so a dataset or prompt file that loads on a UTF-8 machine fails or is "
            "misread elsewhere. A preview rule in ruff 0.15 and 0.16."
        ),
        evidence=(
            "[UKGovernmentBEIS/inspect_evals#2475](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2475): "
            "agentdojo read its suite YAML without an encoding. 77 sites in 30 evaluations "
            "on inspect_evals main."
        ),
        probe='open("data.txt")\n',
    ),
)
"""The curated set. Adding an entry is a minor release, since it reports on repositories that passed before."""

PROBE_FILE = "_inspect_evals_lint_probe.py"
"""The name the probe is linted under: one no ``per-file-ignores`` pattern is likely to single out."""

RUFF_TIMEOUT = 120
"""Seconds one ruff invocation may take."""

_CURATED_PLACEHOLDER = "{curated_rules}"


class RuffError(Exception):
    """Ruff could not answer: it failed, timed out, or printed something that is not JSON."""


def find_ruff() -> str | None:
    """The ruff binary: the ``ruff`` package installed alongside the linter, else ``ruff`` on ``PATH``."""
    try:
        from ruff import find_ruff_bin
    except ImportError:
        return shutil.which("ruff")
    try:
        return find_ruff_bin()
    except FileNotFoundError:
        return shutil.which("ruff")


def _error_text(stderr: str, returncode: int) -> str:
    """The cause ruff gives for a failure: its first ``Cause:`` line, else its last line."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("Cause:"):
            return line.removeprefix("Cause:").strip()
    return lines[-1] if lines else f"ruff exited with status {returncode}"


def _ruff_json(
    binary: str, args: list[str], cwd: Path | None = None, stdin: str | None = None
) -> Any:
    """Run ruff and parse its JSON output.

    Raises:
        RuffError: ruff could not be run, timed out, exited non-zero or printed something else.
    """
    try:
        result = subprocess.run(
            [binary, *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUFF_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuffError(f"ruff took longer than {RUFF_TIMEOUT}s") from e
    except OSError as e:
        raise RuffError(str(e)) from e
    if result.returncode != 0:
        raise RuffError(_error_text(result.stderr, result.returncode))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuffError("ruff printed output that is not JSON") from e


@functools.cache
def _ruff_version(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUFF_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "ruff"
    return result.stdout.strip() or "ruff"


@functools.cache
def _is_preview(binary: str, code: str) -> bool | None:
    """Whether this ruff treats ``code`` as a preview rule; None when it does not know the rule."""
    try:
        data = _ruff_json(binary, ["rule", code, "--output-format", "json"])
    except RuffError:
        return None
    return bool(cast(Mapping[str, object], data).get("preview"))


def _ruff_reads(pyproject: Path) -> bool:
    """Whether ruff takes its configuration from ``pyproject``, or stops on it.

    Ruff reads a ``[tool.ruff]`` table, and fails on a file that is not TOML or
    whose ``tool`` is not a table. A file it fails on is returned as the
    configuration so that ruff's own error explains the skip.
    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return True
    tool: object = data.get("tool", {})
    return not isinstance(tool, dict) or "ruff" in tool


def governing_config(root: Path, directory: Path) -> Path | None:
    """The ruff configuration file for files in ``directory``, or None when there is none.

    Ruff's own discovery: the nearest directory holding ``.ruff.toml``,
    ``ruff.toml`` or a ``pyproject.toml`` with a ``[tool.ruff]`` table, in that
    order of precedence. The search stops at ``root``, so a configuration outside
    the repository, or the user's own, never stands in for the repository's.
    """
    for candidate in (directory, *directory.parents):
        for name in (".ruff.toml", "ruff.toml"):
            if (candidate / name).is_file():
                return candidate / name
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and _ruff_reads(pyproject):
            return pyproject
        if candidate == root:
            break
    return None


def _check_args(isolated: bool) -> list[str]:
    # --no-cache: the repository may not be ours to write to (the register lint
    # service checks out third-party code), and ruff's cache lives in it.
    args = ["check", "--no-cache", "--exit-zero", "--output-format", "json"]
    return [*args, "--isolated"] if isolated else args


def _mtime_ns(config_file: Path | None) -> int | None:
    return config_file.stat().st_mtime_ns if config_file is not None else None


@functools.cache
def _config_error(
    binary: str, root: Path, config_file: Path | None, config_mtime_ns: int | None
) -> str | None:
    """Why ruff cannot load ``config_file`` (its defaults when None), or None when it can.

    Cached by the file's modification time, failures included, so a ruff that
    fails or hangs on a configuration is tried once per run, not once per package.
    """
    directory = config_file.parent if config_file is not None else root
    args = [*_check_args(config_file is None), "--stdin-filename", str(directory / PROBE_FILE), "-"]
    try:
        _ruff_json(binary, args, cwd=root, stdin="")
    except RuffError as e:
        return str(e)
    return None


def _reports(binary: str, entry: CuratedRule, package: Path, root: Path, isolated: bool) -> bool:
    """Whether ruff, configured as it is for ``package``, reports ``entry`` on its probe."""
    args = [*_check_args(isolated), "--stdin-filename", str(package / PROBE_FILE), "-"]
    found = cast(list[Mapping[str, object]], _ruff_json(binary, args, cwd=root, stdin=entry.probe))
    return any(d.get("code") == entry.code for d in found)


@functools.cache
def _violations_at(
    binary: str, code: str, scope: Path, root: Path, isolated: bool, config_mtime_ns: int | None
) -> tuple[int, int] | RuffError:
    try:
        found = cast(
            list[Mapping[str, object]],
            _ruff_json(
                binary,
                [*_check_args(isolated), "--select", code, "--preview", str(scope)],
                cwd=root,
            ),
        )
    except RuffError as e:
        return e
    return len(found), len({d.get("filename") for d in found})


def _violations(
    binary: str, code: str, scope: Path, root: Path, config_file: Path | None
) -> tuple[int, int]:
    """Violations and files ruff reports under ``scope`` with ``code`` selected and preview on.

    Cached by the configuration file's modification time, failures included, so
    packages that share a configuration share one ruff run. A cached count does
    not see later edits to the sources it scanned.

    Raises:
        RuffError: ruff could not count them.
    """
    found = _violations_at(binary, code, scope, root, config_file is None, _mtime_ns(config_file))
    if isinstance(found, RuffError):
        raise found
    return found


def declined_rules(options: Mapping[str, object]) -> dict[str, str]:
    """The ``declined`` option from the rule's table, validated: curated rule code to reason.

    Raises:
        ConfigError: an unknown option, a code outside the curated set, or a missing reason.
    """
    unknown = sorted(set(options) - {"declined"})
    if unknown:
        raise ConfigError(
            f"{OPTION_TABLE}: unknown option(s) {unknown}; the only option is 'declined'"
        )
    value = options.get("declined", {})
    if not isinstance(value, dict):
        raise ConfigError(f"{OPTION_TABLE}: 'declined' must be a table of rule code to reason")
    curated = {entry.code for entry in CURATED_RULES}
    declined: dict[str, str] = {}
    for code, reason in cast(dict[object, object], value).items():
        if not isinstance(code, str) or code not in curated:
            raise ConfigError(
                f"{OPTION_TABLE}: {code!r} in 'declined' is not a curated rule; "
                f"the curated rules are {sorted(curated)}"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ConfigError(f"{OPTION_TABLE}: 'declined.{code}' must give a reason")
        declined[code] = reason.strip()
    return declined


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def _cost(binary: str, code: str, scope: Path, root: Path, config_file: Path | None) -> str:
    """``; enabling it reports N violations in M files under <scope>``, or empty when ruff cannot count."""
    try:
        violations, files = _violations(binary, code, scope, root, config_file)
    except RuffError:
        return ""
    where = _relative(scope, root) if scope != root else "the repository"
    if violations == 0:
        return f"; enabling it reports no violations under {where}"
    return f"; enabling it reports {violations} violation(s) in {files} file(s) under {where}"


def _hint(code: str, preview: bool) -> str:
    if not preview:
        return f'add "{code}" to extend-select'
    prefix = code.rstrip("0123456789")
    return (
        f'add "{code}" to extend-select and set preview = true; a preview rule stays off '
        f'without preview, even when a prefix such as "{prefix}" selects it'
    )


def _documents_curated_rules(fn: RuleFn) -> RuleFn:
    """Fill the docstring's table of curated rules from :data:`CURATED_RULES`, so the page lists what is checked."""
    rows = [
        "| Rule | Why | Evidence |",
        "| --- | --- | --- |",
        *(
            f"| [``{e.code}``](https://docs.astral.sh/ruff/rules/{e.name}/) ``{e.name}`` "
            f"| {e.why} | {e.evidence} |"
            for e in CURATED_RULES
        ),
    ]
    fn.__doc__ = inspect.cleandoc(fn.__doc__ or "").replace(_CURATED_PLACEHOLDER, "\n".join(rows))
    return fn


@rule(
    code="IECQ007",
    name=RULE_NAME,
    category="code_quality",
    scopes=("eval", "helper"),
    summary="The ruff configuration enables the curated ruff rules",
)
@_documents_curated_rules
def ruff_rules_enabled(ctx: LintContext) -> Iterable[Finding]:
    """The ruff configuration enables the curated ruff rules.

    ## What it does
    Warns for each rule in the curated set below that ruff, configured as it is
    for the package, would not report. The finding names the configuration file
    and how many violations ruff would report under the source root if the rule
    were on, so a maintainer can tell a one-line change from a cleanup.

    The check asks ruff rather than reading its configuration. It lints a
    snippet the rule reports as though it were a file in the package, so
    ``select``, ``extend-select``, ``ignore``, ``extend``, ``per-file-ignores``,
    ``preview`` and ``explicit-preview-rules`` all apply exactly as ruff applies
    them. A preview rule that a prefix such as ``PLW`` selects while preview is
    off counts as not enabled, because ruff never reports it. A repository with
    no ruff configuration gets ruff's defaults, which enable none of the curated
    rules.

    It uses the ruff installed alongside the linter, else ``ruff`` on ``PATH``,
    and skips when there is neither, or when ruff cannot load the configuration,
    with ruff's reason. That ruff may be a different version from
    the one the repository pins. Packages that share a configuration share one
    finding: a run reports it under the first of them, and the rest skip.

    ## Why is this bad?
    Each curated rule catches a defect that has reached an evaluation. Ruff
    reports it in the author's editor and pre-commit hook, before review, which a
    custom rule here could not. A preview rule selected only by a prefix is
    silently inert: ruff warns that a selection has no effect only when the code
    is named, so a reader of the configuration would believe it is on.

    ## Curated rules
    {curated_rules}

    ## Example
    ```toml
    [tool.ruff.lint]
    select = ["E", "F", "PLW"]
    ```
    Use instead:
    ```toml
    [tool.ruff.lint]
    select = ["E", "F", "PLW"]
    extend-select = ["PLW1514"]
    preview = true
    ```

    ## Options
    - `ruff_rules_enabled.declined`: a table from curated rule code to the reason the repository does not enable it, e.g. `declined = { PLW1514 = "every file is ASCII" }`. A declined rule that is not enabled is reported as suppressed, with the reason.
    """
    declined = declined_rules(ctx.config.rule_options.get(RULE_NAME, {}))
    binary = find_ruff()
    if binary is None:
        yield Outcome(
            "skip",
            "ruff is not installed alongside the linter or on PATH, so the ruff configuration "
            "cannot be resolved",
        )
        return
    root = ctx.root.absolute()
    package = ctx.path.absolute()
    scope = ctx.config.source_dir(root)
    config_file = governing_config(root, package)
    file = ctx.root / (config_file.relative_to(root) if config_file else "pyproject.toml")
    error = _config_error(binary, root, config_file, _mtime_ns(config_file))
    if error is not None:
        yield Outcome("skip", f"ruff could not resolve its configuration: {error}")
        return
    subject = (
        f"{_relative(config_file, root)} does not enable"
        if config_file is not None
        else "No ruff configuration applies, and ruff's defaults do not enable"
    )
    for entry in CURATED_RULES:
        preview = _is_preview(binary, entry.code)
        if preview is None:
            yield Outcome(
                "skip",
                f"{_ruff_version(binary)} does not know {entry.code}, so whether it is "
                "enabled cannot be checked",
            )
            continue
        try:
            if _reports(binary, entry, package, root, isolated=config_file is None):
                continue
        except RuffError as e:
            yield Outcome("skip", f"ruff could not resolve its configuration: {e}")
            return
        message = f"{subject} ruff rule {entry.code} ({entry.name})"
        if entry.code in declined:
            yield Diagnostic(
                f"{message}; declined: {declined[entry.code]}",
                file=file,
                severity="warning",
                shared=True,
                suppressed=True,
            )
            continue
        yield Diagnostic(
            message + _cost(binary, entry.code, scope, root, config_file),
            file=file,
            severity="warning",
            hint=_hint(entry.code, preview),
            shared=True,
        )
