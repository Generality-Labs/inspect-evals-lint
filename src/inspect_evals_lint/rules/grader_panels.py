"""Preserve role bindings when using Inspect's built-in model graders."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import column_of, end_line_of, parse_failures, parse_python_files
from inspect_evals_lint.rules.host_code import (
    FunctionNode,
    bound_names,
    function_parameters,
    parameter_defaults,
    pattern_captures,
)

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
    The rule follows direct expressions, local assignments and function and lambda
    closures within a file. It recognises imported aliases and merges possible
    assignments across branches. A later unconditional reassignment clears the
    earlier selection. Literal lists and tuples, unpacking and ``typing.cast()``
    retain the selected model.

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
    model_graded_qa(model=get_model(role="grader"))  # inspect-evals-lint: ignore[IEBP017] -- protocol uses the first grader only
    ```

    ## Known limits

    This is bounded source analysis. It does not trace models through helper
    return values, arguments to other functions, attributes, container indexing
    or mutation, or dynamic imports. It checks keyword ``model=`` arguments. Closures are
    analysed using their enclosing scope's final possible assignments; callback
    invocation order and mutations from other functions are not modelled.
    Comprehension loop variables are not captured in deferred functions.
    Role-absence guards use string-literal membership tests against ``model_roles()``;
    equivalent guards through other expressions may need a suppression. Branch
    predicates and exception types are not resolved. Exception handlers receive
    the possible states before statements in the try body; partial evaluation
    within an expression and effects inside called functions are not modelled.
    Loops include both zero and multiple iterations, without proving whether a
    particular iteration count or branch is possible. Match captures inherit
    the subject's possible role selections without resolving pattern elements.
    """
    parsed = parse_python_files(ctx)
    yield from parse_failures(parsed)
    found = False
    for file in parsed.parsed:
        if not isinstance(file.tree, ast.Module):
            continue
        flow = _PanelFlow()
        flow.scope(
            file.tree.body, _State({name: _Value(symbol=symbol) for name, symbol in _APIS.items()})
        )
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


@dataclass(frozen=True)
class _Value:
    roles: Roles = frozenset()
    symbol: str | None = None
    items: tuple[_Value, ...] | None = None


_EMPTY = _Value()


def _values(values: list[_Value]) -> _Value:
    if not values:
        return _EMPTY
    first = values[0]
    items = None
    sequences = [v.items for v in values if v.items is not None]
    if len(sequences) == len(values) and all(len(s) == len(sequences[0]) for s in sequences):
        items = tuple(_values(list(parts)) for parts in zip(*sequences, strict=True))
    return _Value(
        roles=frozenset[str]().union(*(v.roles for v in values)),
        symbol=first.symbol if all(v.symbol == first.symbol for v in values) else None,
        items=items,
    )


def _sequence(items: list[_Value]) -> _Value:
    return _Value(roles=_values(items).roles, items=tuple(items))


@dataclass
class _State:
    values: dict[str, _Value] = field(default_factory=dict)
    unbound: Roles = frozenset()
    defined: frozenset[int] = frozenset()
    """Callbacks defined on this path; earlier exits cannot supply their closure bindings."""

    def copy(self) -> _State:
        return _State(dict(self.values), self.unbound, self.defined)


def _merge(states: list[_State]) -> _State | None:
    if not states:
        return None
    names = set[str]().union(*(s.values.keys() for s in states))
    return _State(
        {name: _values([s.values.get(name, _EMPTY) for s in states]) for name in names},
        states[0].unbound.intersection(*(s.unbound for s in states[1:])),
        frozenset[int]().union(*(s.defined for s in states)),
    )


def _symbol(node: ast.AST, state: _State) -> str | None:
    if isinstance(node, ast.Name):
        return state.values.get(node.id, _EMPTY).symbol
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


def _unpack(targets: list[ast.expr], value: _Value) -> list[_Value]:
    items = value.items
    star = next((i for i, t in enumerate(targets) if isinstance(t, ast.Starred)), None)
    if items is not None:
        if star is None and len(items) == len(targets):
            return list(items)
        if star is not None and len(items) >= len(targets) - 1:
            end = len(items) - (len(targets) - star - 1)
            return [*items[:star], _sequence(list(items[star:end])), *items[end:]]
    return [_Value(value.roles) for _ in targets]


def _bind(target: ast.AST, value: _Value, state: _State) -> None:
    if isinstance(target, ast.Name):
        state.values[target.id] = value
    elif isinstance(target, ast.Starred):
        _bind(target.value, value, state)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item, part in zip(target.elts, _unpack(target.elts, value), strict=True):
            _bind(item, part, state)


ExitKind = Literal["return", "raise", "break", "continue"]


@dataclass
class _Exit:
    kind: ExitKind
    state: _State


@dataclass
class _Result:
    normal: _State | None
    exits: list[_Exit] = field(default_factory=list)


def _results(results: list[_Result]) -> _Result:
    return _Result(
        _merge([r.normal for r in results if r.normal is not None]),
        [e for r in results for e in r.exits],
    )


@dataclass
class _Deferred:
    node: FunctionNode | ast.Lambda
    defaults: dict[str, _Value]


@dataclass
class _Scope:
    deferred: dict[int, _Deferred] = field(default_factory=dict)
    parent: _Scope | None = None
    """Classes schedule methods in the enclosing lexical scope."""

    def add(self, node: FunctionNode | ast.Lambda, defaults: dict[str, _Value]) -> None:
        if self.parent is not None:
            self.parent.add(node, defaults)
        elif previous := self.deferred.get(id(node)):
            previous.defaults = {
                name: _values([value, defaults[name]]) for name, value in previous.defaults.items()
            }
        else:
            self.deferred[id(node)] = _Deferred(node, defaults)


class _PanelFlow:
    def __init__(self) -> None:
        self.findings: dict[int, ast.Call] = {}
        self.scopes: list[_Scope] = []

    def _defer(self, node: FunctionNode | ast.Lambda, state: _State) -> None:
        """Capture defaults when defined, and check the body after enclosing assignments."""
        defaults = {
            arg.arg: self.expression(default, state)
            for arg, default in parameter_defaults(node.args)
        }
        self.scopes[-1].add(node, defaults)
        state.defined |= {id(node)}

    def expression(self, node: ast.AST | None, state: _State) -> _Value:
        if node is None:
            return _EMPTY
        if isinstance(node, ast.Name):
            return state.values.get(node.id, _EMPTY)
        if isinstance(node, ast.Attribute):
            self.expression(node.value, state)
            return _Value(symbol=_symbol(node, state))
        if isinstance(node, ast.IfExp):
            self.expression(node.test, state)
            yes, no = _guard(node.test, state, True), _guard(node.test, state, False)
            value = _values([self.expression(node.body, yes), self.expression(node.orelse, no)])
            merged = _merge([yes, no])
            assert merged is not None
            state.values, state.unbound, state.defined = (
                merged.values,
                merged.unbound,
                merged.defined,
            )
            return value
        if isinstance(node, ast.Lambda):
            self._defer(node, state)
            return _EMPTY
        if isinstance(node, (ast.List, ast.Tuple)):
            parts: list[_Value] = []
            known_items = True
            for item in node.elts:
                value = self.expression(
                    item.value if isinstance(item, ast.Starred) else item, state
                )
                if isinstance(item, ast.Starred):
                    if value.items is None:
                        parts.append(value)
                        known_items = False
                    else:
                        parts.extend(value.items)
                else:
                    parts.append(value)
            return _sequence(parts) if known_items else _Value(_values(parts).roles)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            child = state.copy()
            for generator in node.generators:
                value = self.expression(generator.iter, child)
                _bind(generator.target, _Value(value.roles), child)
                for condition in generator.ifs:
                    self.expression(condition, child)
            values = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            value = _Value(_values([self.expression(v, child) for v in values]).roles)
            state.defined |= child.defined
            return value
        if isinstance(node, ast.NamedExpr):
            value = self.expression(node.value, state)
            _bind(node.target, value, state)
            return value
        if isinstance(node, ast.Call):
            symbol = _symbol(node.func, state)
            self.expression(node.func, state)
            args = [self.expression(arg, state) for arg in node.args]
            keywords = {k.arg: self.expression(k.value, state) for k in node.keywords}
            if symbol in _GRADERS and keywords.get("model", _EMPTY).roles:
                self.findings[id(node)] = node
            if symbol in {"typing.cast", "typing_extensions.cast"}:
                return args[1] if len(args) > 1 else keywords.get("val", _EMPTY)
            if symbol == _APIS["get_model"]:
                role = next((k.value for k in node.keywords if k.arg == "role"), None)
                if role is not None and not (isinstance(role, ast.Constant) and role.value is None):
                    key = ast.dump(role)
                    if key not in state.unbound:
                        return _Value(frozenset({key}))
            return _EMPTY
        values = [self.expression(child, state) for child in ast.iter_child_nodes(node)]
        return _values(values) if isinstance(node, (ast.BoolOp, ast.Await)) else _EMPTY

    def scope(
        self, body: list[ast.stmt], state: _State, *, class_body: bool = False
    ) -> frozenset[int]:
        context = _Scope(parent=self.scopes[-1] if class_body else None)
        self.scopes.append(context)
        result = self.block(body, state)
        self.scopes.pop()
        endings = [
            *([result.normal] if result.normal is not None else []),
            *(e.state for e in result.exits if e.kind == "return"),
        ]
        for deferred in context.deferred.values():
            fn = deferred.node
            enclosing = _merge([s for s in endings if id(fn) in s.defined])
            if enclosing is None:
                continue
            child = enclosing.copy()
            child.unbound = frozenset()
            child.defined = frozenset()
            body = [ast.Return(value=fn.body)] if isinstance(fn, ast.Lambda) else fn.body
            parameters = [a.arg for a in function_parameters(fn)]
            skip = frozenset(
                id(n)
                for n in ast.walk(fn)
                if isinstance(
                    n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
                )
            )
            for name in bound_names(body, parameters, skip=skip):
                child.values.pop(name, None)
            child.values.update(deferred.defaults)
            self.scope(body, child)
        return result.normal.defined if result.normal is not None else frozenset()

    def block(
        self, body: list[ast.stmt], state: _State, exceptions: list[_State] | None = None
    ) -> _Result:
        result = _Result(state)
        for node in body:
            if result.normal is None:
                break
            if exceptions is not None:
                exceptions.append(result.normal.copy())
            step = self.statement(node, result.normal, exceptions)
            result = _Result(step.normal, [*result.exits, *step.exits])
        return result

    def statement(self, node: ast.stmt, state: _State, exceptions: list[_State] | None) -> _Result:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                self.expression(decorator, state)
            self._defer(node, state)
            _bind(ast.Name(id=node.name), _EMPTY, state)
        elif isinstance(node, ast.ClassDef):
            for header in [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]:
                self.expression(header, state)
            state.defined |= self.scope(node.body, state.copy(), class_body=True)
            _bind(ast.Name(id=node.name), _EMPTY, state)
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
                _bind(ast.Name(id=name), _Value(symbol=symbol), state)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = self.expression(node.value, state)
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                _bind(target, value, state)
        elif isinstance(node, (ast.AugAssign, ast.Delete)):
            value = (
                self.expression(node.value, state) if isinstance(node, ast.AugAssign) else _EMPTY
            )
            for target in node.targets if isinstance(node, ast.Delete) else [node.target]:
                previous = (
                    self.expression(target, state) if isinstance(node, ast.AugAssign) else _EMPTY
                )
                _bind(target, _values([previous, value]), state)
        elif isinstance(node, ast.If):
            self.expression(node.test, state)
            return _results(
                [
                    self.block(part, _guard(node.test, state, truth), exceptions)
                    for part, truth in [(node.body, True), (node.orelse, False)]
                ]
            )
        elif isinstance(node, ast.Match):
            return self._match(node, state, exceptions)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            return self._loop(node, state, exceptions)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self.expression(item.context_expr, state)
                if item.optional_vars is not None:
                    _bind(item.optional_vars, _EMPTY, state)
            return self.block(node.body, state, exceptions)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            return self._try(node, state, exceptions)
        elif isinstance(node, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            self.expression(node, state)
            kind: ExitKind = (
                "return"
                if isinstance(node, ast.Return)
                else "raise"
                if isinstance(node, ast.Raise)
                else "break"
                if isinstance(node, ast.Break)
                else "continue"
            )
            return _Result(None, [_Exit(kind, state)])
        else:
            self.expression(node, state)
        return _Result(state)

    def _match(self, node: ast.Match, state: _State, exceptions: list[_State] | None) -> _Result:
        subject = self.expression(node.subject, state)
        branches: list[_Result] = []
        for case in node.cases:
            branch = state.copy()
            for name in pattern_captures(case.pattern):
                branch.values[name] = _Value(subject.roles)
            self.expression(case.guard, branch)
            branches.append(self.block(case.body, branch, exceptions))
            if case.guard is None and _irrefutable(case.pattern):
                break
        else:
            branches.append(_Result(state))
        return _results(branches)

    def _loop(
        self,
        node: ast.For | ast.AsyncFor | ast.While,
        state: _State,
        exceptions: list[_State] | None,
    ) -> _Result:
        """Join bindings from successive iterations until the state stops changing."""
        iterable = self.expression(node.iter, state) if not isinstance(node, ast.While) else _EMPTY
        header = state.copy()
        while True:
            iteration = header.copy()
            if isinstance(node, ast.While):
                self.expression(node.test, iteration)
                iteration = _guard(node.test, iteration, True)
            else:
                _bind(node.target, _Value(iterable.roles), iteration)
            result = self.block(node.body, iteration, exceptions)
            incoming = [state, *(e.state for e in result.exits if e.kind == "continue")]
            if result.normal is not None:
                incoming.append(result.normal)
            merged = _merge(incoming)
            assert merged is not None
            if merged == header:
                break
            header = merged
        exhausted = _guard(node.test, header, False) if isinstance(node, ast.While) else header
        after = self.block(node.orelse, exhausted, exceptions)
        breaks = [e.state for e in result.exits if e.kind == "break"]
        return _Result(
            _merge([*breaks, *([after.normal] if after.normal is not None else [])]),
            [e for e in result.exits if e.kind in ("return", "raise")] + after.exits,
        )

    def _try(
        self, node: ast.Try | ast.TryStar, state: _State, exceptions: list[_State] | None
    ) -> _Result:
        """Keep normal completion and exits separate, then apply finally to each."""
        snapshots: list[_State] = []
        body = self.block(node.body, state.copy(), snapshots)
        success = (
            self.block(node.orelse, body.normal, exceptions)
            if body.normal is not None
            else _Result(None)
        )
        raised = [e.state for e in body.exits if e.kind == "raise"]
        entry = _merge([*snapshots, *raised])
        handlers: list[_Result] = []
        if entry is not None:
            for handler in node.handlers:
                branch = entry.copy()
                self.expression(handler.type, branch)
                if handler.name:
                    branch.values[handler.name] = _EMPTY
                result = self.block(handler.body, branch, exceptions)
                if handler.name:
                    for finished in [
                        *([result.normal] if result.normal is not None else []),
                        *(e.state for e in result.exits),
                    ]:
                        finished.values.pop(handler.name, None)
                handlers.append(result)
        combined = _results([success, *handlers])
        combined.exits.extend(body.exits)
        if entry is not None:
            combined.exits.append(_Exit("raise", entry))
        if exceptions is not None:
            exceptions.extend(snapshots)
        final = _Result(None)
        if combined.normal is not None:
            final = self.block(node.finalbody, combined.normal, exceptions)
        kinds: tuple[ExitKind, ...] = ("return", "raise", "break", "continue")
        for kind in kinds:
            ending = _merge([e.state for e in combined.exits if e.kind == kind])
            if ending is None:
                continue
            cleanup = self.block(node.finalbody, ending, exceptions)
            final.exits.extend(cleanup.exits)
            if cleanup.normal is not None:
                final.exits.append(_Exit(kind, cleanup.normal))
        return final


def _irrefutable(pattern: ast.pattern) -> bool:
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _irrefutable(pattern.pattern)
    return isinstance(pattern, ast.MatchOr) and any(_irrefutable(p) for p in pattern.patterns)
