"""Dataset pinning: a dataset load or raw asset URL names an immutable commit, not a branch."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import (
    column_of,
    end_line_of,
    get_call_name,
    parse_failures,
    parse_python_files,
)

LOADER_NAMES = ("hf_dataset", "load_dataset", "snapshot_download")
"""Dataset loaders whose ``revision=`` decides which commit is read."""

LOADER_MODULES = frozenset(
    {
        "datasets",
        "huggingface_hub",
        "inspect_ai.dataset",
        "inspect_ai.dataset._sources.hf",
    }
)
"""Modules the loaders are defined in.

A call is checked when the name resolves to one of these. A same-named function
imported from a local module is a wrapper that may pin the revision itself, so it
is left alone, as is a method called on an object.
"""

IDENTIFIER_KEYWORDS = ("path", "repo_id", "dataset_name")
"""Keywords carrying the dataset identifier, for the diagnosis and the allowlist key."""

COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}")
"""A full Git commit SHA. Short SHAs and tags are moving refs for this rule, as ADR-0004 says."""

FLOATING_REFS = frozenset({"main", "master"})
"""Branch names a raw GitHub asset URL is never pinned to."""

RAW_GITHUB = re.compile(
    r"https?://raw\.githubusercontent\.com/(?P<org>[^/\s]+)/(?P<repo>[^/\s]+)/(?P<ref>[^/\s?#]+)(?:[/?#]\S*)?",
    re.IGNORECASE,
)
"""A raw GitHub asset URL: the ref segment decides whether it is pinned."""


@dataclass
class Imports:
    """The names a module binds by importing, so a call can be resolved to its definition."""

    names: dict[str, str] = field(default_factory=dict)
    """Local name to qualified path: ``{"load_dataset": "datasets.load_dataset"}``.

    A relative import stores ``""`` for the local name, marking it as local code
    whose definition the rule cannot see.
    """

    def resolve(self, expression: str) -> str:
        """The qualified path behind ``expression``, or ``""`` when it is not imported."""
        root, _, rest = expression.partition(".")
        bound = self.names.get(root)
        if bound is None:
            return ""
        return bound + (f".{rest}" if rest else "")


@rule(
    code="IEBP016",
    name="dataset_pinning",
    category="best_practices",
    scopes=("eval", "helper"),
    allowlist=True,
    summary="Dataset loads name an immutable commit SHA, and raw asset URLs are pinned to one",
    references=(
        inspect_docs("datasets", "Datasets: Hugging Face Datasets", "sec-hugging-face-datasets"),
    ),
)
def dataset_pinning(ctx: LintContext) -> Iterable[Finding]:
    """Dataset loads name an immutable commit SHA, and raw asset URLs are pinned to one.

    ## What it does
    Flags each ``hf_dataset(...)``, ``load_dataset(...)`` or ``snapshot_download(...)``
    call imported from ``datasets``, ``inspect_ai.dataset`` or ``huggingface_hub``
    with no ``revision=`` keyword, and each whose ``revision=`` is a string literal
    that is not a full 40-character hex SHA (``"main"``, ``"v1.2"``, a short SHA).
    A ``revision=`` that is a name, attribute or call is accepted, since the rule
    cannot see its value.

    A same-named function imported from a local module is a wrapper that may pin
    the revision itself, and a method call such as ``self.load_dataset()`` is not
    a loader call at all; neither is flagged.

    Also flags each raw ``raw.githubusercontent.com/<org>/<repo>/main/...`` or
    ``/master/...`` URL, except in a module, class or function docstring, where it
    is normally a citation rather than an asset the eval downloads.

    Each finding is keyed by the dataset identifier or the URL, so an allowlist
    entry can name one asset while the rest of the repository stays pinned.

    ## Why is this bad?
    A branch or tag resolves to whatever upstream holds when the eval runs. A push
    upstream then changes the samples, and therefore the scores, with nothing in
    the repository or the log to show it. A raw ``main`` URL has the same problem
    and can change under a force-push. Pinning to a commit SHA makes the run
    reproducible and the comparison between two runs meaningful.

    ## Example
    ```python
    from inspect_ai.dataset import hf_dataset

    dataset = hf_dataset(path="allenai/ai2_arc", split="test")
    ```
    Use instead:
    ```python
    dataset = hf_dataset(
        path="allenai/ai2_arc",
        split="test",
        revision="2bb88cae8e6b4b1c0a8b2d1e5b3b9a0e4b1c2d3e",
    )
    ```

    ## Options
    - `allowlists.dataset_pinning`: `{ package = ["allenai/ai2_arc"] }` entries reported as warnings while they stay unpinned.
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    findings: list[Diagnostic] = []
    loaders = 0
    urls = 0

    for parsed in parsed_files.parsed:
        imports = _collect_imports(parsed.tree)
        docstrings = _docstring_constants(parsed.tree)
        for node in ast.walk(parsed.tree):
            if isinstance(node, ast.Call):
                if not _is_loader_call(node, imports):
                    continue
                loaders += 1
                finding = _check_loader(node, parsed.path)
                if finding is not None:
                    findings.append(finding)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                match = RAW_GITHUB.search(node.value)
                if match is None:
                    continue
                urls += 1
                if match.group("ref").lower() in FLOATING_REFS:
                    findings.append(_floating_url(node, match, parsed.path))

    yield from findings

    if findings:
        return
    if loaders or urls:
        yield Outcome(
            "pass",
            f"All {loaders} dataset load(s) and {urls} raw asset URL(s) name an immutable commit",
        )
    else:
        yield Outcome("skip", "No dataset loads or raw GitHub asset URLs found")


def _collect_imports(tree: ast.AST) -> Imports:
    imports = Imports()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imports.names[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom):
            module = "" if node.level else (node.module or "")
            for alias in node.names:
                qualified = f"{module}.{alias.name}" if module else ""
                imports.names[alias.asname or alias.name] = qualified
    return imports


def _is_loader_call(call: ast.Call, imports: Imports) -> bool:
    """Whether the call resolves to a loader defined in one of ``LOADER_MODULES``."""
    func = call.func
    if isinstance(func, ast.Name):
        resolved = imports.names.get(func.id, "")
    elif isinstance(func, ast.Attribute):
        if func.attr not in LOADER_NAMES:
            return False
        resolved = imports.resolve(ast.unparse(func.value))
        resolved = f"{resolved}.{func.attr}" if resolved else ""
    else:
        return False

    module, _, name = resolved.rpartition(".")
    return module in LOADER_MODULES and name in LOADER_NAMES


def _docstring_constants(tree: ast.AST) -> set[int]:
    """The ``id()`` of every module, class and function docstring constant."""
    constants: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                constants.add(id(body[0].value))
    return constants


def _check_loader(call: ast.Call, file: Path) -> Diagnostic | None:
    name = get_call_name(call)
    subject = _dataset_subject(call)
    revision = _keyword(call, "revision")

    if revision is None or (isinstance(revision, ast.Constant) and revision.value is None):
        return Diagnostic(
            f"{name}() loads {subject} without revision=, so a change upstream moves "
            "the samples and the scores with no signal in the log",
            file=file,
            line=call.lineno,
            column=column_of(call),
            end_line=end_line_of(call),
            hint='pass revision="<40-character commit SHA>"',
            key=_subject_key(call),
        )

    if (
        isinstance(revision, ast.Constant)
        and isinstance(revision.value, str)
        and not COMMIT_SHA.fullmatch(revision.value)
    ):
        return Diagnostic(
            f"{name}() loads {subject} at revision={revision.value!r}, which is "
            "not a commit SHA; a branch or tag changes the samples between runs",
            file=file,
            line=call.lineno,
            column=column_of(revision),
            end_line=end_line_of(revision),
            hint="pin revision= to a full 40-character commit SHA",
            key=_subject_key(call),
        )

    return None


def _floating_url(node: ast.Constant, match: re.Match[str], file: Path) -> Diagnostic:
    url = match.group(0)
    ref = match.group("ref")
    return Diagnostic(
        f"raw GitHub asset URL {url!r} points at floating ref {ref!r}, so the asset "
        "can change between runs",
        file=file,
        line=node.lineno,
        column=column_of(node),
        end_line=end_line_of(node),
        hint="pin the URL to a full 40-character commit SHA",
        key=url,
    )


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _dataset_subject(call: ast.Call) -> str:
    literal = _identifier_literal(call)
    return f"'{literal}'" if literal else "a dataset"


def _subject_key(call: ast.Call) -> str | None:
    return _identifier_literal(call)


def _identifier_literal(call: ast.Call) -> str | None:
    """The dataset identifier as a literal, for the message and the allowlist key.

    The first positional argument, or the identifier keyword (``path=``,
    ``repo_id=``, ``dataset_name=``), when it is a string literal. Anything else
    is unknown to a static check and becomes ``None``.
    """
    candidates: list[ast.expr] = []
    if call.args:
        candidates.append(call.args[0])
    for keyword_name in IDENTIFIER_KEYWORDS:
        value = _keyword(call, keyword_name)
        if value is not None:
            candidates.append(value)

    for candidate in candidates:
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
    return None
