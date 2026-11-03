"""Best-practice rule: a judge's verdict is read from an anchored label, not by substring or an unanchored one-token regex."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome, Severity
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import column_of, end_line_of, parse_failures, parse_python_files
from inspect_evals_lint.rules.code_extraction import is_test_file

VOCABULARY = frozenset(
    {"yes", "no", "y", "n", "true", "false", "correct", "incorrect", "pass", "fail"}
)
"""The verdict words a judge is asked for. A word outside this set is a task word and is not read."""

_RE_READERS = frozenset({"search", "findall", "finditer", "compile"})
"""``re.match`` and ``re.fullmatch`` read from the start of the string, so they are anchored."""
_TEXT_METHODS = frozenset(
    {
        "lower",
        "upper",
        "strip",
        "lstrip",
        "rstrip",
        "casefold",
        "replace",
        "removeprefix",
        "removesuffix",
    }
)
_SPLIT_METHODS = frozenset({"split", "rsplit", "splitlines"})
"""Containment in a split string is membership in its words or lines, not a substring search."""

_LABELLED_TOKEN = re.compile(r"[A-Za-z_][\w ]*\s*[:>=]\s*(\w+)")
"""``answer: yes`` or ``answer>yes``: a label, a separator and a verdict word."""
_INLINE_FLAGS = re.compile(r"^(?:\(\?[aiLmsux]+\))+")
_WHITESPACE_MATCHERS = re.compile(r"\\s[*+?]?|\[ \\t\][*+?]?|[ \t]+")
_BOUNDARIES = re.compile(r"\\[bB]|\(\?<?[=!][^()]*\)")
"""Word boundaries and lookarounds, which decorate a token without anchoring it."""
_LABEL_PREFIX = re.compile(r"^[A-Za-z_]\w*[:>=]")
_GROUP = re.compile(r"^\((?:\?:|\?P<\w+>)?([^()]*)\)$")
"""One group, capturing or not, wrapping the whole pattern: ``(yes|no)``, ``(?P<grade>[ABC])``."""
_ONE_CHARACTER_CLASS = re.compile(r"^\[[A-Za-z0-9]{2,}\]$")
_LABELLED_NUMBER = re.compile(
    r"^[A-Za-z_][\w ]*(?:\\s[*+?]?|[ \t])*[:=](?:\\s[*+?]?|[ \t])*"
    r"\(((?:[^()]|\([^()]*\))*)\)$"
)
"""A label, a separator and one capturing group: ``Ratio:\\s*(\\d+(?:\\.\\d+)?)``."""
_NUMERIC_GROUP = re.compile(r"^(?=.*\\d)[\\d.\[\]+*?():,]+$")
"""A group that can only capture a number: digits, a decimal point and quantifiers."""

_VERDICT_HINT = (
    "read the verdict from the label the prompt names, anchored to the start of a line and "
    "at the position the prompt asks for (first for a verdict-first prompt, last for a "
    "reason-then-verdict prompt), and treat no match as a grader parse failure; where the "
    "parser reproduces the upstream benchmark on purpose, suppress with a reason"
)
_NUMBER_HINT = (
    "anchor the label to the start of a line and take the match where the prompt puts it, "
    "since a judge can quote the model's own labelled line; for a checker program, read its "
    "exit code first and take a score only from an accepted run"
)

_Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class _Site:
    node: ast.AST
    message: str
    severity: Severity
    hint: str


def _own_nodes(body: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Every node under ``body``, leaving out nested functions, classes and lambdas."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _scopes(tree: ast.Module) -> Iterator[list[ast.AST]]:
    """The module's own statements, then the body of each function, class and lambda, at any depth."""
    yield list(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield list(node.body)
        elif isinstance(node, ast.Lambda):
            yield [node.body]


def _unwrap_text_methods(node: ast.expr) -> ast.expr:
    """``x.strip().lower()`` reads as ``x``."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _TEXT_METHODS
    ):
        node = node.func.value
    return node


def _is_model_output(node: ast.expr) -> bool:
    """``state.output``, or an attribute of it such as ``state.output.completion``."""
    node = _unwrap_text_methods(node)
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "output"
            and isinstance(node.value, ast.Name)
            and node.value.id == "state"
        ):
            return True
        node = node.value
    return False


def _model_output_names(nodes: list[ast.AST]) -> set[str]:
    """Names in a scope bound only to the model under test's output."""
    values: dict[str, list[bool]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target] if node.value is not None else []
        elif isinstance(node, ast.NamedExpr):
            targets = [node.target]
        else:
            continue
        for target in targets:
            values.setdefault(target.id, []).append(
                node.value is not None and _is_model_output(node.value)
            )
    return {name for name, flags in values.items() if all(flags)}


def _verdict_token(text: str) -> str | None:
    """The verdict word a string literal tests for, or None."""
    word = text.strip().lower()
    if word in VOCABULARY:
        return word
    labelled = _LABELLED_TOKEN.fullmatch(word)
    if labelled and labelled.group(1) in VOCABULARY:
        return labelled.group(1)
    return None


def _is_split(node: ast.expr) -> bool:
    node = _unwrap_text_methods(node)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SPLIT_METHODS
    )


def _reads_model_output(node: ast.expr, model_output: set[str]) -> bool:
    """Whether ``node`` is the model under test's output, directly or through a name bound only to it."""
    if _is_model_output(node):
        return True
    unwrapped = _unwrap_text_methods(node)
    return isinstance(unwrapped, ast.Name) and unwrapped.id in model_output


def _containment_sites(nodes: list[ast.AST], model_output: set[str]) -> Iterator[_Site]:
    for node in nodes:
        if not isinstance(node, ast.Compare) or not isinstance(node.ops[0], (ast.In, ast.NotIn)):
            continue
        if not isinstance(node.left, ast.Constant) or not isinstance(node.left.value, str):
            continue
        token = _verdict_token(node.left.value)
        if token is None:
            continue
        container = node.comparators[0]
        if isinstance(container, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Constant)):
            continue
        if _is_split(container) or _reads_model_output(container, model_output):
            continue
        operator = "not in" if isinstance(node.ops[0], ast.NotIn) else "in"
        yield _Site(
            node,
            f'Verdict token "{node.left.value}" tested by containment ({operator}), which '
            "matches it inside other words and anywhere in the judge's reasoning",
            "error",
            _VERDICT_HINT,
        )


def _pattern_text(node: ast.expr) -> str | None:
    """The text of a pattern literal; an f-string's expressions read as NUL. None for anything else."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "\0"
            for part in node.values
        )
    return None


def _is_anchored(pattern: str) -> bool:
    r"""Whether the pattern pins the match to a line or the string: ``^``, ``$``, ``\A`` or ``\Z``.

    A word boundary (``\b``, ``\B``) or a lookaround is not an anchor: it stops a
    match inside a word, but still finds the token anywhere in the reasoning.
    """
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\" and i + 1 < len(pattern):
            if pattern[i + 1] in "AZ":
                return True
            i += 2
            continue
        if char == "[":
            i += 1
            if i < len(pattern) and pattern[i] == "^":
                i += 1
            if i < len(pattern) and pattern[i] == "]":
                i += 1
            while i < len(pattern) and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
            continue
        if char in "^$":
            return True
        i += 1
    return False


def _is_verdict_pattern(pattern: str) -> bool:
    """A verdict word, an alternation of them, or a one-character class, with at most a label before it."""
    core = _INLINE_FLAGS.sub("", pattern)
    core = _BOUNDARIES.sub("", core)
    core = _WHITESPACE_MATCHERS.sub("", core)
    core = _LABEL_PREFIX.sub("", core)
    group = _GROUP.match(core)
    inner = group.group(1) if group else core
    if inner.lower() in VOCABULARY or _ONE_CHARACTER_CLASS.match(inner):
        return True
    options = [option.strip().lower() for option in inner.split("|")]
    return len(options) > 1 and (
        all(option in VOCABULARY for option in options)
        or all(len(option) == 1 and option.isalnum() for option in options)
    )


def _is_labelled_number_pattern(pattern: str) -> bool:
    labelled = _LABELLED_NUMBER.match(_BOUNDARIES.sub("", _INLINE_FLAGS.sub("", pattern)))
    return labelled is not None and _NUMERIC_GROUP.match(labelled.group(1)) is not None


def _re_reader(call: ast.Call) -> tuple[str, ast.expr, ast.expr | None] | None:
    """``re.<reader>(pattern, string)``: the callee's display name, its pattern and the text it reads."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "re"
        and func.attr in _RE_READERS
    ):
        return None
    keywords = {k.arg: k.value for k in call.keywords if k.arg}
    patterns = call.args[:1] or [keywords["pattern"]] if "pattern" in keywords else call.args[:1]
    if not patterns:
        return None
    strings = call.args[1:2] or ([keywords["string"]] if "string" in keywords else [])
    return f"re.{func.attr}()", patterns[0], strings[0] if strings else None


def _regex_sites(nodes: list[ast.AST], model_output: set[str]) -> Iterator[_Site]:
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        reader = _re_reader(node)
        if reader is None:
            continue
        callee, argument, text = reader
        pattern = _pattern_text(argument)
        if pattern is None or _is_anchored(pattern):
            continue
        if text is not None and _reads_model_output(text, model_output):
            continue
        if _is_verdict_pattern(pattern):
            yield _Site(
                node,
                f"{callee} with the unanchored verdict pattern {pattern!r}, which matches the "
                "token anywhere in the reply, including inside other words and in reasoning",
                "error",
                _VERDICT_HINT,
            )
        elif _is_labelled_number_pattern(pattern):
            yield _Site(
                node,
                f"{callee} reads a labelled number with the unanchored pattern {pattern!r} "
                "from the whole text, which can include a line the model under test wrote",
                "warning",
                _NUMBER_HINT,
            )


@rule(
    code="IEBP015",
    name="verdict_parse_anchored",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="A judge's verdict is read from an anchored label, not by substring or an unanchored one-token regex",
    references=(
        inspect_docs(
            "scoring-policy", "Scoring Policy: Model-graded scorers", "model-graded-scorers"
        ),
        inspect_docs(
            "scoring-policy",
            "Scoring Policy: Attribution: whose output failed?",
            "attribution-whose-output-failed",
        ),
    ),
)
def verdict_parse_anchored(ctx: LintContext) -> Iterable[Finding]:
    r"""A judge's verdict is read from an anchored label, not by substring or an unanchored one-token regex.

    ## What it does
    Flags two ways of reading a verdict that match it anywhere in the text, and
    warns on a third:

    1. A verdict word tested by containment: `"yes" in response`, `"no" not in
       reply.lower()`, or the word after a label, as in `"answer: yes" in text`.
       The words are a fixed vocabulary: yes, no, y, n, true, false, correct,
       incorrect, pass and fail, in any case. A test against a literal tuple,
       list, set or dictionary, or against the words or lines of a split
       string, is a membership check, not a search, and passes.
    2. `re.search`, `re.findall`, `re.finditer` or `re.compile` whose pattern
       is one of those words, an alternation of them such as `(yes|no)`, or a
       one-character choice such as `[01]` or `(A|B|C)`, with at most a label
       such as `correct:` before it, and no anchor. An anchor is `^`, `$`,
       `\A` or `\Z`, which pins the match to a line or to the string. A word
       boundary (`\b`) or a lookaround is not one: it stops a match inside a
       word, but still finds the token anywhere in the reasoning. `re.match`
       and `re.fullmatch` read from the start of the string, which is the
       leading token a verdict-first prompt asks for, and are not flagged. A
       compiled pattern is reported once, where it is compiled, including in
       a class body.
    3. As a warning, a search over the whole output for a labelled number such
       as `Ratio:\s*(\d+)`, with no anchor.

    Text read from `state.output`, directly or through a name bound only to
    it, is the model under test's, whose format may be part of what the task
    measures, and is left alone in all three shapes. Only `re` called by that
    name is read; an alias or the `regex` module is not. Test files are not
    read.

    ## Why is this bad?
    A judge is asked to reason and then give a verdict, and its reasoning uses
    the verdict words as ordinary words. `"yes" in reply` matches "yesterday";
    `re.search(r"y", reply)` matches the first "y" in any sentence;
    `re.search(r"correct: (yes|no)", reply)` matches the tail of
    `incorrect: yes` and inverts the verdict; `re.search(r"[01]", reply)` reads
    the "1" in "Sentence 1". Each one turns a right grade into a wrong one on
    ordinary judge prose, in either direction, without any error.

    A word boundary does not repair this. `re.findall(r"\b(yes|no)\b", reply)`
    no longer matches "yesterday", but it still collects every "yes" and "no"
    the reasoning uses as ordinary words, and whichever one the code takes may
    not be the verdict. In a study of nine judge models from 2023 to 2026,
    each grading the same 435 answers, parsers that required the verdict on a
    labelled line disagreed between the first and the last match on at most
    0.2% of replies. Parsers that searched for a bare verdict word anywhere
    disagreed on 17% to 33%, because the reasoning named both words.

    The fix is to read the verdict from the label the prompt names, anchored to
    the start of a line (allowing emphasis around the label and a list marker
    before it), at the position the prompt asks for: the leading token for a
    verdict-first prompt, the last labelled line for a reason-then-verdict
    prompt. When nothing matches, that is a grader parse failure, not a
    verdict. A judge that returns several fields is better asked for a
    structured reply, through a tool call with a schema.

    A deterministic checker, such as a test runner, can quote the model's own
    output back in an error message. A search for `Ratio:\s*(\d+)` over its
    whole output then reads a number the model printed. Read the checker's exit
    code first, take the score only from an accepted run, and anchor the label
    to the line the checker writes it on.

    Where a parser reproduces the upstream benchmark's own reading on purpose,
    suppress the finding with a reason that says so.

    ## Example
    ```python
    async def score(state: TaskState, target: Target) -> Score:
        reply = (await grader.generate(prompt)).completion
        match = re.search(r"correct: (yes|no)", reply)
        verdict = match.group(1) if match else "no"
        ...
    ```
    Use instead:
    ```python
    VERDICT = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\**correct\**:\s*\**(yes|no)\b", re.I | re.M)


    async def score(state: TaskState, target: Target) -> Score:
        reply = (await grader.generate(prompt)).completion
        matches = VERDICT.findall(reply)
        if not matches:
            return Score.unscored(
                reason="grader_failed", metadata={"grader_failure_mode": "grader_parse_failure"}
            )
        verdict = matches[-1].lower()
        ...
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    found = False
    for parsed in parsed_files.parsed:
        if is_test_file(parsed.path, ctx.path):
            continue
        assert isinstance(parsed.tree, ast.Module)
        sites: list[_Site] = []
        for body in _scopes(parsed.tree):
            nodes = list(_own_nodes(body))
            model_output = _model_output_names(nodes)
            sites.extend(_containment_sites(nodes, model_output))
            sites.extend(_regex_sites(nodes, model_output))
        for site in sorted(
            sites, key=lambda s: (getattr(s.node, "lineno", 0), getattr(s.node, "col_offset", 0))
        ):
            found = True
            yield Diagnostic(
                site.message,
                file=parsed.path,
                line=getattr(site.node, "lineno", None),
                column=column_of(site.node),
                end_line=end_line_of(site.node),
                severity=site.severity,
                hint=site.hint,
            )
    if not found:
        yield Outcome("pass", "No verdict read by substring or by an unanchored pattern")
