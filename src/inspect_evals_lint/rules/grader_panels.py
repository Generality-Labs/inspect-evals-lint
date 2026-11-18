"""Preserve role bindings when using Inspect's built-in model graders."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import column_of, end_line_of, parse_failures, parse_python_files
from inspect_evals_lint.rules.host_code import FunctionNode, scope_nodes

_APIS = {
    "get_model": "inspect_ai.model.get_model",
    "model_roles": "inspect_ai.model.model_roles",
    "model_graded_qa": "inspect_ai.scorer.model_graded_qa",
    "model_graded_fact": "inspect_ai.scorer.model_graded_fact",
}
_GRADERS = {_APIS["model_graded_qa"], _APIS["model_graded_fact"]}
Roles = frozenset[str]


@rule(
    code="IEBP017",
    name="grader_panel_preservation",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Preserve configured grader panels when calling built-in model graders",
    references=(
        inspect_docs("models", "Models: Multiple Models per Role", "multiple-models-per-role"),
        inspect_docs("model-graded", "Model Grading"),
    ),
)
def grader_panel_preservation(ctx: LintContext) -> Iterable[Finding]:
    """Preserve configured grader panels when calling built-in model graders.

    ## What it does

    Warns when a model selected by ``get_model(role=...)`` is passed to the
    ``model=`` argument of ``model_graded_qa()`` or ``model_graded_fact()``.
    The rule follows direct expressions, local assignments and named nested-function
    closures within a file. It recognises imported aliases and merges possible
    assignments across branches. A later unconditional reassignment clears the
    earlier selection.

    ## Why is this bad?

    ``get_model(role=...)`` returns one model, even when the runner binds a list
    to the role. Passing that result as ``model=`` prevents the built-in scorer
    from resolving the full panel and applying its aggregation.

    ## Example

    ```python
    async def score(state, target):
        grader = get_model(role="grader", default=DEFAULT_GRADER)
        return await model_graded_qa(model=grader)(state, target)
    ```

    Use instead, inside the running scoring function:

    ```python
    async def score(state, target):
        model = None if "grader" in model_roles() else DEFAULT_GRADER
        return await model_graded_qa(model=model)(state, target)
    ```

    This defers a configured role to the built-in scorer and uses the default
    only when the role is absent. To require the role, use
    ``model_graded_qa(model_role=ModelRole("grader", required=True))``.
    Passing the full role binding is also allowed. A lookup guarded by
    ``"grader" not in model_roles()`` is allowed because no panel is bound on
    that branch. See the
    [grader-role guidance](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/BEST_PRACTICES.md#use-model-roles-for-multi-model-evals).

    Bare built-in scorer calls are allowed. Defaults and required roles are
    separate policy choices. A scorer that intentionally selects one panel
    member can use the normal suppression with a reason:

    ```python
    model_graded_qa(model=get_model(role="grader"))  # inspect-evals-lint: ignore[IEBP017] - protocol uses the first grader only
    ```

    ## Known limits

    This is bounded source analysis. It does not trace models through helper
    return values, arguments to other functions, attributes, containers or
    dynamic imports. It checks keyword ``model=`` arguments. Closures are
    analysed using their enclosing scope's final possible assignments; callback
    invocation order and mutations from other functions are not modelled.
    Role-absence guards use string-literal membership tests against ``model_roles()``;
    equivalent guards through other expressions may need a suppression.
    """
    parsed = parse_python_files(ctx)
    yield from parse_failures(parsed)
    found = False
    for file in parsed.parsed:
        if not isinstance(file.tree, ast.Module):
            continue
        flow = _PanelFlow()
        flow.scope(file.tree.body, _State(symbols=dict(_APIS)))
        for call in sorted(flow.findings.values(), key=lambda n: n.lineno):
            found = True
            yield Diagnostic(
                "get_model(role=...) selects one model before the built-in grader can resolve its panel",
                file=file.path,
                line=call.lineno,
                column=column_of(call),
                end_line=end_line_of(call),
                severity="warning",
                hint="let the built-in scorer resolve the role; choose a fallback inside score() only when the role is unbound",
            )
    if not found:
        yield Outcome("pass", "No single-model role selection passed to a built-in grader")


@dataclass
class _State:
    values: dict[str, Roles] = field(default_factory=dict)
    symbols: dict[str, str] = field(default_factory=dict)
    unbound: Roles = frozenset()

    def copy(self) -> _State:
        return _State(dict(self.values), dict(self.symbols), self.unbound)


def _merge(states: list[_State]) -> _State:
    merged = states[0].copy()
    for state in states[1:]:
        for name in merged.values.keys() | state.values.keys():
            merged.values[name] = merged.values.get(name, frozenset()) | state.values.get(
                name, frozenset()
            )
        merged.symbols = {
            name: value
            for name, value in merged.symbols.items()
            if state.symbols.get(name) == value
        }
        merged.unbound &= state.unbound
    return merged


def _symbol(node: ast.AST, state: _State) -> str | None:
    if isinstance(node, ast.Name):
        return state.symbols.get(node.id)
    if isinstance(node, ast.Attribute) and (base := _symbol(node.value, state)):
        return f"{base}.{node.attr}"
    return None


def _guard(test: ast.expr, state: _State, truth: bool) -> _State:
    branch = state.copy()
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _guard(test.operand, state, not truth)
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.left, ast.Constant)
        and isinstance(test.left.value, str)
        and isinstance(test.comparators[0], ast.Call)
        and _symbol(test.comparators[0].func, state) == _APIS["model_roles"]
        and (
            (isinstance(test.ops[0], ast.NotIn) and truth)
            or (isinstance(test.ops[0], ast.In) and not truth)
        )
    ):
        branch.unbound |= {ast.dump(test.left)}
    return branch


def _bind(target: ast.AST, value: Roles, state: _State, symbol: str | None = None) -> None:
    if isinstance(target, ast.Name):
        state.values[target.id] = value
        state.symbols.pop(target.id, None)
        if symbol is not None:
            state.symbols[target.id] = symbol
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _bind(item, frozenset(), state)


class _PanelFlow:
    def __init__(self) -> None:
        self.findings: dict[int, ast.Call] = {}

    def expression(self, node: ast.AST | None, state: _State) -> Roles:
        if node is None:
            return frozenset()
        if isinstance(node, ast.Name):
            return state.values.get(node.id, frozenset())
        if isinstance(node, ast.IfExp):
            self.expression(node.test, state)
            return self.expression(node.body, _guard(node.test, state, True)) | self.expression(
                node.orelse, _guard(node.test, state, False)
            )
        if isinstance(node, ast.Lambda):
            return frozenset()
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            child = state.copy()
            for generator in node.generators:
                self.expression(generator.iter, child)
                _bind(generator.target, frozenset(), child)
                for condition in generator.ifs:
                    self.expression(condition, child)
            for value in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                self.expression(value, child)
            return frozenset()
        if isinstance(node, ast.NamedExpr):
            value = self.expression(node.value, state)
            _bind(node.target, value, state)
            return value
        if isinstance(node, ast.Call):
            symbol = _symbol(node.func, state)
            for arg in node.args:
                self.expression(arg, state)
            for keyword in node.keywords:
                value = self.expression(keyword.value, state)
                if symbol in _GRADERS and keyword.arg == "model" and value:
                    self.findings[id(node)] = node
            self.expression(node.func, state)
            if symbol == _APIS["get_model"]:
                role = next((k.value for k in node.keywords if k.arg == "role"), None)
                if role is not None and not (isinstance(role, ast.Constant) and role.value is None):
                    key = ast.dump(role)
                    if key not in state.unbound:
                        return frozenset({key})
            return frozenset()
        values = [self.expression(child, state) for child in ast.iter_child_nodes(node)]
        if isinstance(node, (ast.BoolOp, ast.Await)):
            return frozenset[str]().union(*values)
        return frozenset()

    def scope(
        self, body: list[ast.stmt], state: _State, class_parent: _State | None = None
    ) -> None:
        nested: list[tuple[FunctionNode, _State]] = []
        exits: list[_State] = []
        final = self.block(body, state, nested, exits)
        enclosing = class_parent or _merge(
            [*exits, *([final] if final is not None else [])] or [state]
        )
        for function, definition in nested:
            child = enclosing.copy()
            child.unbound = frozenset()
            args = function.args
            parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            parameters.extend(a for a in [args.vararg, args.kwarg] if a is not None)
            local_names = {a.arg for a in parameters}
            for node in scope_nodes(
                function.body,
                skip=frozenset(
                    id(child)
                    for child in ast.walk(function)
                    if isinstance(
                        child,
                        (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
                    )
                ),
            ):
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    local_names.add(node.id)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    local_names.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    local_names.update((a.asname or a.name).split(".")[0] for a in node.names)
            for name in local_names:
                child.values.pop(name, None)
                child.symbols.pop(name, None)
            positional = [*args.posonlyargs, *args.args]
            defaults = list(
                zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
            )
            defaults.extend(
                (arg, default)
                for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                if default is not None
            )
            for arg, default in defaults:
                child.values[arg.arg] = self.expression(default, definition)
            self.scope(function.body, child)

    def block(
        self,
        body: list[ast.stmt],
        state: _State,
        nested: list[tuple[FunctionNode, _State]],
        exits: list[_State],
    ) -> _State | None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for header in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
                    self.expression(header, state)
                nested.append((node, state.copy()))
                _bind(ast.Name(id=node.name), frozenset(), state)
            elif isinstance(node, ast.ClassDef):
                for header in [
                    *node.decorator_list,
                    *node.bases,
                    *(k.value for k in node.keywords),
                ]:
                    self.expression(header, state)
                self.scope(node.body, state.copy(), class_parent=state.copy())
                _bind(ast.Name(id=node.name), frozenset(), state)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    symbol = (
                        f"{node.module}.{alias.name}"
                        if isinstance(node, ast.ImportFrom)
                        else alias.name
                        if alias.asname
                        else name
                    )
                    _bind(ast.Name(id=name), frozenset(), state, symbol)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.expression(node.value, state)
                symbol = _symbol(node.value, state) if node.value is not None else None
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    _bind(target, value, state, symbol)
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                self.expression(node, state)
                for target in node.targets if isinstance(node, ast.Delete) else [node.target]:
                    _bind(target, frozenset(), state)
            elif isinstance(node, ast.If):
                self.expression(node.test, state)
                branches = [
                    self.block(part, _guard(node.test, state, truth), nested, exits)
                    for part, truth in [(node.body, True), (node.orelse, False)]
                ]
                live = [branch for branch in branches if branch is not None]
                if not live:
                    return None
                state = _merge(live)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                self.expression(node.test if isinstance(node, ast.While) else node.iter, state)
                iteration = state.copy()
                if not isinstance(node, ast.While):
                    _bind(node.target, frozenset(), iteration)
                loop = self.block(node.body, iteration, nested, exits)
                state = _merge([state, *([loop] if loop is not None else [])])
                after = self.block(node.orelse, state, nested, exits)
                if after is None:
                    return None
                state = after
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    self.expression(item.context_expr, state)
                    if item.optional_vars is not None:
                        _bind(item.optional_vars, frozenset(), state)
                after = self.block(node.body, state, nested, exits)
                if after is None:
                    return None
                state = after
            elif isinstance(node, (ast.Try, ast.TryStar)):
                success = self.block(node.body + node.orelse, state.copy(), nested, exits)
                branches = [
                    self.block(handler.body, state.copy(), nested, exits)
                    for handler in node.handlers
                ]
                state = _merge([state, *[s for s in [success, *branches] if s is not None]])
                after = self.block(node.finalbody, state, nested, exits)
                if after is None:
                    return None
                state = after
            elif isinstance(node, (ast.Return, ast.Raise)):
                self.expression(node, state)
                exits.append(state.copy())
                return None
            else:
                self.expression(node, state)
        return state
