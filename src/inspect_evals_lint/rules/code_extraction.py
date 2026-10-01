"""Code-quality rule: extract code from a completion with the shared helper, not a hand-rolled fence pattern."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from inspect_evals_lint.context import LintContext, is_package
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import (
    column_of,
    end_line_of,
    get_call_name,
    parse_failures,
    parse_python_files,
)
from inspect_evals_lint.rules.dependencies import declared_dependencies

HELPER = "extract_code_block"
HELPER_DISTRIBUTION = "inspect-evals"
"""The distribution that ships ``inspect_evals.utils.code.extract_code_block``, in PEP 503 form."""

_RE_FUNCTIONS = frozenset({"compile", "search", "findall", "finditer", "match", "fullmatch", "sub"})
_STR_METHODS = frozenset({"split", "replace", "find", "partition", "startswith"})
_TAG_FUNCTION = "extract_from_tags"

_FENCE = re.compile(r"```|`\{3,?\d*\}")
"""A literal triple backtick, or a regex quantifier asking for three of them."""
_LABELLED_FENCE = re.compile(
    r"(?:```|`\{3,?\d*\})(?:\s|\\s[*+?]?)*(?:\((?:\?:)?(?P<group>[\w\\+#|-]+)\)|(?P<word>[A-Za-z][\w+#-]*))"
)
"""A fence with its label: a word (```` ```python ````) or a group of alternatives (```` ```(?:cuda|cpp)? ````)."""
_BARE_FENCE_FILLER = re.compile(r"```|`\{3,?\d*\}|\\[ns]|[\s?*+^$]")
"""What a pattern matching only a fence may also hold: optional newlines and whitespace, anchors."""

_HINT = (
    f"use inspect_evals.utils.code.{HELPER}(completion, language) and bump the task version, "
    "since the extracted text can change; if the upstream benchmark takes a different block, "
    "suppress with a reason"
)

_Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class _FenceCall:
    call: ast.Call
    callee: str
    json: bool
    """Every fence label in the pattern is ``json``."""
    bare: bool
    """The pattern is a fence on its own, such as a closing fence being stripped."""


def _labels(text: str) -> list[str]:
    """The labels on the fences in ``text``; a group of alternatives gives each one."""
    labels: list[str] = []
    for match in _LABELLED_FENCE.finditer(text):
        if match["word"]:
            labels.append(match["word"])
        else:
            labels.extend(part.strip() for part in match["group"].split("|") if part.strip())
    return labels


def _is_json(text: str) -> bool:
    labels = _labels(text)
    return bool(labels) and all(label.lower() == "json" for label in labels)


def _is_bare(text: str) -> bool:
    return not _labels(text) and not _BARE_FENCE_FILLER.sub("", text)


def _helper_modules(ctx: LintContext) -> set[Path]:
    """Files in the repository's helper packages that define ``extract_code_block`` at top level."""
    source_dir = ctx.config.source_dir(ctx.root)
    found: set[Path] = set()
    for name in ctx.config.helper_dirs:
        package = source_dir / name
        if not is_package(package):
            continue
        for path in package.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
                if f"def {HELPER}" in text and any(
                    isinstance(node, _Function) and node.name == HELPER
                    for node in ast.parse(text).body
                ):
                    found.add(path)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
    return found


def _is_test_file(path: Path, package: Path) -> bool:
    relative = path.relative_to(package) if path.is_relative_to(package) else Path(path.name)
    return (
        relative.name.startswith("test_")
        or relative.name.endswith("_test.py")
        or relative.name == "conftest.py"
        or any(part in ("test", "tests") for part in relative.parts[:-1])
    )


def _literal_text(
    node: ast.expr, resolve: Callable[[str], str | None] = lambda _: None
) -> str | None:
    r"""The constant text of a string literal, f-string or ``+`` concatenation; None for anything else.

    A name inside a concatenation reads as ``resolve(name)``. Whatever is not
    constant reads as NUL, so ``rf"```{tag}\n"`` is a fence with an unknown label.
    """
    if isinstance(node, ast.Name):
        return resolve(node.id)
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "\0"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_text(node.left, resolve), _literal_text(node.right, resolve)
        if left is None and right is None:
            return None
        return (left or "\0") + (right or "\0")
    return None


def _bindings(scope: ast.Module | _Function) -> dict[str, list[str | None]]:
    """Names bound in ``scope`` (not in nested functions or classes), each with its literal values.

    A binding whose value is not a literal, a parameter included, is recorded as
    None, so a local name shadows a module constant of the same name.
    """
    found: dict[str, list[str | None]] = {}
    if not isinstance(scope, ast.Module):
        arguments = scope.args
        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            found.setdefault(arg.arg, []).append(None)
    stack: list[ast.AST] = list(scope.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            pairs = [(node.target, node.value)]
        else:
            pairs = []
        for target, value in pairs:
            if isinstance(target, ast.Name):
                text = _literal_text(value) if value is not None else None
                found.setdefault(target.id, []).append(text)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _calls_by_scope(tree: ast.Module) -> Iterator[tuple[_Function | None, ast.Call]]:
    """Every call with its innermost enclosing function; None at module level."""

    def visit(
        node: ast.AST, scope: _Function | None
    ) -> Iterator[tuple[_Function | None, ast.Call]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _Function):
                yield from visit(child, child)
                continue
            if isinstance(child, ast.Call):
                yield scope, child
            yield from visit(child, scope)

    yield from visit(tree, None)


def _pattern_arguments(call: ast.Call) -> tuple[str, list[ast.expr]] | None:
    """The callee's display name and the arguments that hold a pattern or separator; None for other calls."""
    func = call.func
    if isinstance(func, ast.Attribute):
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "re"
            and func.attr in _RE_FUNCTIONS
        ):
            pattern = call.args[:1] or [k.value for k in call.keywords if k.arg == "pattern"]
            return f"re.{func.attr}()", pattern
        if func.attr in _STR_METHODS:
            return f"str.{func.attr}()", call.args[:1]
    if get_call_name(call) == _TAG_FUNCTION:
        tags = [k.value for k in call.keywords if k.arg in ("start_tag", "end_tag")]
        return f"{_TAG_FUNCTION}()", [*call.args[1:], *tags]
    return None


def _literal_values(bindings: dict[str, list[str | None]], name: str) -> list[str]:
    return [value for value in bindings.get(name, []) if value is not None]


def _single_value(bindings: dict[str, list[str | None]], name: str) -> str | None:
    """The literal ``name`` is bound to, when it is bound exactly once."""
    values = bindings.get(name, [])
    return values[0] if len(values) == 1 else None


def _fence_calls(tree: ast.Module) -> list[tuple[_Function | None, _FenceCall]]:
    """Calls passing a fence literal, directly or through a name bound in the same function or module."""
    module_bindings = _bindings(tree)
    local_bindings: dict[int, dict[str, list[str | None]]] = {}
    found: list[tuple[_Function | None, _FenceCall]] = []
    for scope, call in _calls_by_scope(tree):
        matched = _pattern_arguments(call)
        if matched is None:
            continue
        callee, arguments = matched
        local: dict[str, list[str | None]] = {}
        if scope is not None:
            if id(scope) not in local_bindings:
                local_bindings[id(scope)] = _bindings(scope)
            local = local_bindings[id(scope)]
        visible = {**module_bindings, **local}
        texts: list[str] = []
        for argument in arguments:
            if isinstance(argument, ast.Name):
                texts.extend(_literal_values(visible, argument.id))
            elif (text := _literal_text(argument, partial(_single_value, visible))) is not None:
                texts.append(text)
        fences = [text for text in texts if _FENCE.search(text)]
        if callee.startswith("str."):
            # A string method sees no capture group, so only a labelled opening fence
            # marks extraction; a bare "```" is as likely a toggle while reading markdown.
            fences = [
                text for text in fences if any(label.lower() != "json" for label in _labels(text))
            ]
        if fences:
            found.append(
                (
                    scope,
                    _FenceCall(
                        call,
                        callee,
                        json=all(_is_json(text) for text in fences),
                        bare=all(_is_bare(text) for text in fences),
                    ),
                )
            )
    return found


def _sites(tree: ast.Module) -> list[tuple[_Function | None, list[_FenceCall]]]:
    """Fence calls grouped by enclosing function, with JSON parsing left out.

    A call whose fences are all labelled ``json`` is JSON parsing. A bare fence,
    such as a closing fence being stripped, is part of the same clean-up when its
    function has such a call. Module-level calls, typically compiled patterns for
    a function below, are grouped the same way, one site per file.
    """
    groups: dict[int, tuple[_Function | None, list[_FenceCall]]] = {}
    for scope, fence_call in _fence_calls(tree):
        groups.setdefault(id(scope), (scope, []))[1].append(fence_call)
    sites: list[tuple[_Function | None, list[_FenceCall]]] = []
    for scope, calls in groups.values():
        parses_json = any(c.json for c in calls)
        kept = [c for c in calls if not c.json and not (c.bare and parses_json)]
        if kept:
            sites.append((scope, sorted(kept, key=lambda c: (c.call.lineno, c.call.col_offset))))
    return sorted(sites, key=lambda site: (site[1][0].call.lineno, site[1][0].call.col_offset))


@rule(
    code="IECQ006",
    name="shared_code_extraction",
    category="code_quality",
    scopes=("eval", "helper"),
    summary="Code is extracted from a completion with inspect_evals' extract_code_block, not a hand-rolled fence pattern",
)
def shared_code_extraction(ctx: LintContext) -> Iterable[Finding]:
    r"""Code is extracted from a completion with ``inspect_evals.utils.code.extract_code_block``, not a hand-rolled fence pattern.

    ## What it does
    Flags a string literal holding a triple-backtick fence, or a regex
    quantifier asking for three backticks, passed as the pattern to
    ``re.compile``, ``search``, ``findall``, ``finditer``, ``match``,
    ``fullmatch`` or ``sub``, or as a tag to ``extract_from_tags``. As the
    separator to a ``split``, ``replace``, ``find``, ``partition`` or
    ``startswith`` method it counts only with a language label, such as
    ``"```python"``: a bare fence there is as likely a toggle while reading
    markdown. The literal may be an f-string or a ``+`` concatenation, or a name
    bound to one in the same function or at module level. One diagnostic per
    function, at its first such call, and one for the module-level calls in a
    file.

    A pattern whose fences are all labelled ``json`` is JSON parsing, not code
    extraction, and is left alone. So is a bare fence, such as a closing fence
    being stripped, beside one in the same function. A label alternation that
    names another language, such as ``(?:json|python)?``, still counts. Test
    files and the module that defines the helper are not read.

    The rule applies only where the helper is available: in inspect_evals itself,
    where a helper package (``helper-dirs``) defines ``extract_code_block``, and in
    a repository whose ``pyproject.toml`` declares a dependency on
    ``inspect-evals``. Elsewhere it is skipped.

    ## Why is this bad?
    Hand-rolled fence patterns disagree on the edge cases: prose before the fence,
    an unlabelled fence, more than one block, a fence that does not start a line,
    CRLF line endings. Two evaluations of the same model then extract different
    code from the same kind of answer, and a bug fixed in one stays in the rest.
    ``extract_code_block(completion, language)`` matches only fences at the start
    of a line, prefers a block labelled with the requested language over an
    unlabelled one, returns the first matching block, and returns ``None`` when
    nothing matches, so the caller decides what an answer without a block means.

    Migrating changes which text is extracted for some completions: a fence in
    the middle of a line no longer counts, and the first matching block wins
    where a pattern may have taken another. Scores can move, so a migration needs
    a comparability bump to the task version and a changelog entry.

    Which block to take is still open. Most upstream harnesses take the first,
    but that choice has not been tested against how current models answer, and
    UKGovernmentBEIS/inspect_evals#2454 is re-evaluating it. Until then, keep
    each evaluation's current choice. Migrate where the helper takes the same
    block. Where the evaluation takes a different block (the last one, say) or
    chooses by content, keep its logic and suppress the finding with a reason
    that names its choice.

    ## Example
    ```python
    def find_code(completion: str) -> str:
        pattern = re.compile(r"```python\n(.*?)```", re.DOTALL)
        matches = pattern.findall(completion)
        return matches[0] if matches else completion
    ```
    Use instead:
    ```python
    from inspect_evals.utils.code import extract_code_block


    def find_code(completion: str) -> str:
        return extract_code_block(completion, "python") or completion
    ```
    Or, where the upstream benchmark extracts differently:
    ```python
    blocks = re.findall(r"```cpp\n(.*?)```", text, re.DOTALL)  # inspect-evals-lint: ignore[IECQ006] -- upstream takes the last block with #include
    ```
    """
    helper_modules = _helper_modules(ctx)
    if not helper_modules and HELPER_DISTRIBUTION not in declared_dependencies(ctx.root):
        yield Outcome(
            "skip",
            f"inspect_evals.utils.code.{HELPER} is not available: no helper package defines it "
            f"and pyproject.toml does not depend on {HELPER_DISTRIBUTION}",
        )
        return

    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    found = False
    for parsed in parsed_files.parsed:
        if parsed.path in helper_modules or _is_test_file(parsed.path, ctx.path):
            continue
        assert isinstance(parsed.tree, ast.Module)
        for scope, calls in _sites(parsed.tree):
            found = True
            first = calls[0]
            where = f"{scope.name}()" if scope is not None else "module-level code"
            more = sorted({c.call.lineno for c in calls[1:]} - {first.call.lineno})
            also = f"; more at line(s) {', '.join(map(str, more))}" if more else ""
            yield Diagnostic(
                f"Hand-rolled code-fence extraction in {where}: {first.callee} with a fence "
                f"pattern{also}",
                file=parsed.path,
                line=first.call.lineno,
                column=column_of(first.call),
                end_line=end_line_of(first.call),
                severity="warning",
                hint=_HINT,
            )
    if not found:
        yield Outcome("pass", "No hand-rolled code-fence extraction found")
