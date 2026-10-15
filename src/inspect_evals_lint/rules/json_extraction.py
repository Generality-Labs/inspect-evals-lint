"""Code-quality rule: strip a code fence from a generated reply before parsing it as strict JSON."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import column_of, end_line_of, parse_failures, parse_python_files
from inspect_evals_lint.rules.code_extraction import (
    HELPER_DISTRIBUTION,
    helper_modules,
    is_test_file,
)
from inspect_evals_lint.rules.dependencies import declared_dependencies

HELPER = "strip_json_fence"

_STRICT_METHODS = frozenset({"model_validate_json", "validate_json"})

_HINT = (
    f"pass the reply through inspect_evals.utils.code.{HELPER}() first, so JSON wrapped in a "
    "code fence still parses, and bump the task version, since replies that failed to parse "
    "can now be graded; if the upstream benchmark rejects fenced replies, suppress with a reason"
)

_Function = ast.FunctionDef | ast.AsyncFunctionDef
_ExprTest = Callable[[ast.expr | None], bool]


def _own_nodes(scope: _Function) -> Iterator[ast.AST]:
    """Every node in ``scope``'s body, leaving out nested functions, classes and lambdas."""
    stack: list[ast.AST] = list(scope.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _functions(tree: ast.Module) -> Iterator[_Function]:
    for node in ast.walk(tree):
        if isinstance(node, _Function):
            yield node


def _bindings(scope: _Function) -> dict[str, list[ast.expr | None]]:
    """Names bound in ``scope``, each with the values assigned; None for a parameter or other binding."""
    found: dict[str, list[ast.expr | None]] = {}
    arguments = scope.args
    for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
        found.setdefault(arg.arg, []).append(None)
    for node in _own_nodes(scope):
        targets: list[ast.AST]
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ) or isinstance(node, ast.AnnAssign | ast.NamedExpr):
            targets = [node.targets[0] if isinstance(node, ast.Assign) else node.target]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.For | ast.AsyncFor):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        else:
            continue
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    found.setdefault(name.id, []).append(value)
    return found


def _is_generate(node: ast.expr | None) -> bool:
    """``await <model>.generate(...)``, or the call without the ``await``."""
    if isinstance(node, ast.Await):
        node = node.value
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "generate"
    )


def _bound_only_to(bindings: dict[str, list[ast.expr | None]], test: _ExprTest) -> set[str]:
    return {name for name, values in bindings.items() if values and all(test(v) for v in values)}


def _completion_test(outputs: set[str]) -> _ExprTest:
    def is_completion(node: ast.expr | None) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "completion"
            and (
                (isinstance(node.value, ast.Name) and node.value.id in outputs)
                or _is_generate(node.value)
            )
        )

    return is_completion


def _strict_parser(call: ast.Call) -> str | None:
    """The display name of a strict JSON parse, or None for any other call."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in _STRICT_METHODS:
        return f"{func.attr}()"
    if func.attr == "loads" and isinstance(func.value, ast.Name) and func.value.id == "json":
        return "json.loads()"
    return None


def _sites(scope: _Function) -> list[tuple[ast.Call, str]]:
    """Strict JSON parses in ``scope`` whose argument is the completion of a ``generate`` call."""
    bindings = _bindings(scope)
    is_completion = _completion_test(_bound_only_to(bindings, _is_generate))
    completions = _bound_only_to(bindings, is_completion)
    found: list[tuple[ast.Call, str]] = []
    for node in _own_nodes(scope):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        callee = _strict_parser(node)
        argument = node.args[0]
        if callee is not None and (
            is_completion(argument)
            or (isinstance(argument, ast.Name) and argument.id in completions)
        ):
            found.append((node, callee))
    return sorted(found, key=lambda site: (site[0].lineno, site[0].col_offset))


@rule(
    code="IECQ008",
    name="fence_tolerant_json",
    category="code_quality",
    scopes=("eval", "helper"),
    summary="A generated reply is passed through inspect_evals' strip_json_fence before strict JSON parsing",
)
def fence_tolerant_json(ctx: LintContext) -> Iterable[Finding]:
    """A generated reply is passed through ``inspect_evals.utils.code.strip_json_fence`` before strict JSON parsing.

    ## What it does
    Flags ``json.loads``, a pydantic model's ``model_validate_json`` or a
    ``TypeAdapter``'s ``validate_json`` called on the ``completion`` of a
    ``generate`` call made in the same function: either the attribute itself,
    or a name bound only to it. The ``generate`` result may be bound to a name
    or used inline. One diagnostic per call. A completion that reaches the parse
    through any other call, such as ``strip_json_fence`` or a project's own
    extraction, is left alone. So is the output of the model under test, read
    from ``state.output``, whose format may be part of what the task measures.
    Test files are not read.

    The rule applies only where the helper is available: in inspect_evals itself,
    where a helper package (``helper-dirs``) defines ``strip_json_fence``, and in
    a repository whose ``pyproject.toml`` declares a dependency on
    ``inspect-evals``. Elsewhere it is skipped.

    ## Why is this bad?
    A model asked for bare JSON, even through a ``response_schema``, sometimes
    wraps it in a Markdown code fence. Some providers accept a
    ``response_schema`` without enforcing it, and which one serves a request can
    change from one call to the next. A strict parser rejects the fenced reply,
    so a correct grade is thrown away and the sample is scored as a grader
    failure, or as whatever the fallback is. ``strip_json_fence(text)`` returns
    the JSON inside one fence, labelled ``json`` or unlabelled, that wraps the
    whole reply, and returns any other text unchanged, so a reply with prose
    around the JSON still fails as before.

    Stripping the fence can turn a failed parse into a grade, so scores can
    move, and a fix needs a comparability bump to the task version and a
    changelog entry. Suppress the finding, with a reason that names the rule,
    only where the upstream benchmark rejects fenced replies too.

    ## Example
    ```python
    async def score(state: TaskState, target: Target) -> Score:
        result = await get_model(role="grader").generate(prompt, config=config)
        verdict = Verdict.model_validate_json(result.completion)
        ...
    ```
    Use instead:
    ```python
    from inspect_evals.utils.code import strip_json_fence


    async def score(state: TaskState, target: Target) -> Score:
        result = await get_model(role="grader").generate(prompt, config=config)
        verdict = Verdict.model_validate_json(strip_json_fence(result.completion))
        ...
    ```
    """
    helpers = helper_modules(ctx, HELPER)
    if not helpers and HELPER_DISTRIBUTION not in declared_dependencies(ctx.root):
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
        if parsed.path in helpers or is_test_file(parsed.path, ctx.path):
            continue
        assert isinstance(parsed.tree, ast.Module)
        for scope in _functions(parsed.tree):
            for call, callee in _sites(scope):
                found = True
                yield Diagnostic(
                    f"Strict JSON parse of a generated reply in {scope.name}(): {callee} on the "
                    "completion, which rejects JSON wrapped in a code fence",
                    file=parsed.path,
                    line=call.lineno,
                    column=column_of(call),
                    end_line=end_line_of(call),
                    severity="warning",
                    hint=_HINT,
                )
    if not found:
        yield Outcome("pass", "No strict JSON parse of a generated reply found")
