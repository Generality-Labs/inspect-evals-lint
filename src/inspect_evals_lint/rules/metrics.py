"""Custom metrics and reducers: what a metric may assume about the scores it is handed.

Inspect reduces each sample's epoch scores to one before any metric sees them,
even at ``epochs=1``. The analysis reads one ``@metric`` or ``@score_reducer``
function at a time, including everything nested in it. Within that function a
name holds a score value if any assignment to it reads one, and refers to
the scores the function was handed from where it is bound to them until it is
rebound. Nothing is followed into other functions or files.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome, Severity
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import (
    column_of,
    end_line_of,
    get_call_name,
    get_decorator_name,
    parse_failures,
    parse_python_files,
)
from inspect_evals_lint.rules.host_code import annotation_names

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

_NARROWING_METHODS = frozenset({"as_int", "as_bool"})
_VALUE_ACCESSORS = frozenset({"as_float", "as_int", "as_bool", "as_str", "as_list", "as_dict"})
_NOT_A_VALUE = frozenset({"len", "isinstance", "bool", "int", "str", "type", "id"})
"""Calls whose result says something about a score value without being one."""
_DROPPED_FIELDS = frozenset({"answer", "explanation", "reason"})
_SCORE_TYPES = frozenset({"SampleScore", "Score"})
_MUTATORS = frozenset({"append", "extend", "insert", "update", "add", "setdefault"})
"""Methods that store their arguments in the object they are called on."""
_PASS_THROUGH = frozenset({"enumerate", "reversed", "sorted", "zip", "list", "iter", "filter"})
"""Calls whose result is new but yields the same objects as their arguments."""


@dataclass(frozen=True)
class _Scope:
    """A ``@metric`` or ``@score_reducer`` function and what it is given."""

    node: FunctionNode
    kind: Literal["metric", "reducer"]
    reduced: bool
    """Whether the scores it reads have been through an epoch reducer: a metric not declared ``scores="unreduced"``."""


def _decorated_scopes(tree: ast.AST) -> list[_Scope]:
    scopes: list[_Scope] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            name = get_decorator_name(decorator)
            if name == "metric":
                scopes.append(_Scope(node, "metric", reduced=not _declares_unreduced(decorator)))
            elif name == "score_reducer":
                scopes.append(_Scope(node, "reducer", reduced=False))
    return scopes


def _declares_unreduced(decorator: ast.expr) -> bool:
    return isinstance(decorator, ast.Call) and any(
        keyword.arg == "scores"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value == "unreduced"
        for keyword in decorator.keywords
    )


def _bindings(node: ast.AST) -> Iterator[tuple[ast.expr, ast.expr]]:
    """``(target, value)`` pairs where ``node`` moves a value into a name or adds it to one."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield target, node.value
    elif isinstance(node, ast.AnnAssign):
        if node.value is not None:
            yield node.target, node.value
    elif isinstance(node, ast.AugAssign | ast.NamedExpr):
        yield node.target, node.value
    elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
        yield node.target, node.iter
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATORS
    ):
        for arg in [*node.args, *(k.value for k in node.keywords)]:
            yield node.func.value, arg


def _target_names(target: ast.expr) -> Iterator[str]:
    """Names a binding target assigns or fills: ``x``, ``a, b``, and ``x`` in ``x.append(...)``."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _target_names(element)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _is_value_attribute(node: ast.AST) -> bool:
    """``<x>.score.value`` or ``<name>.value``, read rather than assigned."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.ctx, ast.Load)
        and (
            isinstance(node.value, ast.Name)
            or (isinstance(node.value, ast.Attribute) and node.value.attr == "score")
        )
    )


def _is_narrowing(node: ast.AST) -> bool:
    """``int(...)``, ``.as_int()`` or ``.as_bool()``."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == "int"
    return isinstance(node.func, ast.Attribute) and node.func.attr in _NARROWING_METHODS


class _Values:
    """Which names in one scope hold a score value, or something computed from one."""

    def __init__(self, scope: FunctionNode) -> None:
        self.names: set[str] = set()
        self.direct: set[str] = set()
        """Names bound to the value itself, or an item of it, rather than to a computation over it."""
        bindings = [pair for node in ast.walk(scope) for pair in _bindings(node)]
        changed = True
        while changed:
            changed = False
            for target, value in bindings:
                if _is_narrowing(value) or not self.reads_value(value):
                    continue
                for name in self._value_names(target, value):
                    if name not in self.names:
                        self.names.add(name)
                        changed = True
        for target, value in bindings:
            if isinstance(target, ast.Name) and self.is_value(value):
                self.direct.add(target.id)

    def _value_names(self, target: ast.expr, value: ast.expr) -> Iterator[str]:
        """Names in ``target`` that receive something computed from a score value, not a key or index beside it.

        ``k, v`` over ``value.items()`` and ``i, v`` over ``enumerate(values)``
        give ``v``; ``zip()`` and a tuple pair targets with values element by
        element. Any other unpacking gives nothing.
        """
        if isinstance(target, ast.Name):
            yield target.id
            return
        if not isinstance(target, ast.Tuple | ast.List):
            return
        pairs: list[tuple[ast.expr, ast.expr]] = []
        elements = target.elts
        if isinstance(value, ast.Tuple | ast.List) and len(value.elts) == len(elements):
            pairs = list(zip(elements, value.elts, strict=True))
        elif isinstance(value, ast.Call):
            name = get_call_name(value)
            method = isinstance(value.func, ast.Attribute)
            if method and name == "items" and len(elements) == 2:
                yield from _target_names(elements[1])
            elif not method and name == "enumerate" and len(elements) == 2 and value.args:
                pairs = [(elements[1], value.args[0])]
            elif not method and name == "zip" and len(value.args) == len(elements):
                pairs = list(zip(elements, value.args, strict=True))
        for element, item in pairs:
            if not _is_narrowing(item) and self.reads_value(item):
                yield from self._value_names(element, item)

    def reads_value(self, node: ast.expr) -> bool:
        """Whether ``node`` computes something from a score value: ``sum(v)`` does, ``len(v)`` and ``v == 1`` do not."""
        if _is_value_attribute(node) or (isinstance(node, ast.Name) and node.id in self.names):
            return True
        if isinstance(node, ast.Compare):
            return False
        if isinstance(node, ast.Call):
            name = get_call_name(node)
            if isinstance(node.func, ast.Name) and name in _NOT_A_VALUE:
                return False
            if isinstance(node.func, ast.Attribute) and name in _VALUE_ACCESSORS:
                return True
        return any(
            self.reads_value(child)
            for child in ast.iter_child_nodes(node)
            if isinstance(child, ast.expr)
        )

    def is_value(self, node: ast.expr) -> bool:
        """Whether ``node`` is a score value or an item of one: ``s.score.value``, ``value["key"]``, ``cast(T, v)``."""
        while True:
            if isinstance(node, ast.Subscript):
                node = node.value
            elif (
                isinstance(node, ast.Call) and get_call_name(node) == "cast" and len(node.args) == 2
            ):
                node = node.args[1]
            else:
                break
        return _is_value_attribute(node) or (isinstance(node, ast.Name) and node.id in self.direct)


def _chain(node: ast.expr) -> tuple[str | None, list[ast.expr]]:
    """The name an attribute or subscript chain starts from, and the links after it, outermost last.

    ``s.score.metadata["k"]`` gives ``s`` and the ``s.score``, ``s.score.metadata``
    and ``s.score.metadata["k"]`` nodes.
    """
    links: list[ast.expr] = []
    while isinstance(node, ast.Attribute | ast.Subscript):
        links.insert(0, node)
        node = node.value
    return (node.id if isinstance(node, ast.Name) else None), links


def _first_field(links: list[ast.expr]) -> str | None:
    first = links[0] if links else None
    return first.attr if isinstance(first, ast.Attribute) else None


def _returned_functions(fn: FunctionNode) -> set[str]:
    """Names of the functions a factory returns: ``metric`` in ``def metric(...): ...; return metric``."""
    returned: set[str] = set()
    for statement in ast.walk(fn):
        if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name):
            returned.add(statement.value.id)
    return returned


@dataclass(frozen=True)
class _Alias:
    """How a name reaches the scores a scope was handed."""

    copied: bool = False
    """A shallow copy, or a new list of the originals: its own fields or slots are new, the objects they hold are not."""
    replaced: frozenset[str] = frozenset()
    """Fields of a shallow copy that ``model_copy(update={...})`` set to new objects."""

    def written_by(self, links: list[ast.expr]) -> bool:
        """Whether a store through ``links`` reaches an object the scope was handed."""
        if not self.copied:
            return True
        return len(links) >= 2 and _first_field(links) not in self.replaced

    def merge(self, other: _Alias) -> _Alias:
        """What a name may be after two branches: the one that reaches more of the originals."""
        if not (self.copied and other.copied):
            return _Alias()
        return _Alias(copied=True, replaced=self.replaced & other.replaced)


_State = dict[str, _Alias]


def _merge(*states: _State) -> _State:
    merged: _State = {}
    for state in states:
        for name, alias in state.items():
            merged[name] = merged[name].merge(alias) if name in merged else alias
    return merged


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


class _Writes:
    """Stores into the scores a scope was handed, read in statement order.

    A name refers to the scores from the point it is bound to them, a loop
    variable over them or an object inside them, until it is rebound to
    something else. After an ``if``, a loop or a ``try`` it refers to them if
    any branch left it so. A shallow ``model_copy()`` or ``copy.copy()`` is new
    itself but shares what it holds, so a store into one of its fields reaches
    the original; ``model_copy(deep=True)``, ``copy.deepcopy()`` and any other
    call give a new object.
    """

    def __init__(self, scope: FunctionNode) -> None:
        self.returned = _returned_functions(scope)
        self.found: dict[int, ast.expr] = {}
        self.block(scope.body, {})

    def inputs(self, fn: FunctionNode) -> set[str]:
        """The parameters of ``fn`` that receive scores: the first one of a returned function, and any annotated with a score type."""
        params = [*fn.args.posonlyargs, *fn.args.args]
        inputs = {params[0].arg} if fn.name in self.returned and params else set[str]()
        for param in [*params, *fn.args.kwonlyargs]:
            if annotation_names(param.annotation) & _SCORE_TYPES:
                inputs.add(param.arg)
        return inputs

    def alias(self, value: ast.expr, state: _State) -> _Alias | None:
        """How ``value`` reaches the scores, or None when it is a new object."""
        if isinstance(value, ast.Call):
            return self.call_alias(value, state)
        if isinstance(value, ast.ListComp | ast.GeneratorExp):
            targets = {
                name for generator in value.generators for name in _target_names(generator.target)
            }
            element, _ = _chain(value.elt)
            if element in targets and any(
                self.alias(generator.iter, state) is not None for generator in value.generators
            ):
                return _Alias(copied=True)
            return None
        root, links = _chain(value)
        found = state.get(root) if root is not None else None
        if found is None or not links:
            return found
        if found.copied and _first_field(links) in found.replaced:
            return None
        return _Alias()

    def call_alias(self, call: ast.Call, state: _State) -> _Alias | None:
        func = call.func
        if isinstance(func, ast.Name) and func.id in _PASS_THROUGH:
            if any(self.alias(arg, state) is not None for arg in call.args):
                return _Alias(copied=True)
            return None
        if isinstance(func, ast.Attribute) and func.attr == "model_copy":
            keywords = {k.arg: k.value for k in call.keywords}
            if "deep" in keywords and not isinstance(keywords["deep"], ast.Constant):
                return None
            if "deep" in keywords and _is_true(keywords["deep"]):
                return None
            update = keywords.get("update")
            if update is not None and not (
                isinstance(update, ast.Dict)
                and all(
                    isinstance(k, ast.Constant) and isinstance(k.value, str) for k in update.keys
                )
            ):
                return None
            replaced = frozenset(
                k.value
                for k in (update.keys if isinstance(update, ast.Dict) else [])
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
            source = self.alias(func.value, state)
            if source is None:
                return None
            return _Alias(copied=True, replaced=source.replaced | replaced)
        shallow_copy = (
            isinstance(func, ast.Attribute)
            and func.attr == "copy"
            and isinstance(func.value, ast.Name)
            and func.value.id == "copy"
        ) or (isinstance(func, ast.Name) and func.id == "copy")
        if shallow_copy and len(call.args) == 1 and self.alias(call.args[0], state) is not None:
            return _Alias(copied=True)
        return None

    def bind(self, target: ast.expr, value: ast.expr | None, state: _State) -> None:
        """Record what ``target`` refers to after it is assigned ``value``; None for a value nobody can see."""
        if isinstance(target, ast.Tuple | ast.List):
            if isinstance(value, ast.Tuple | ast.List) and len(value.elts) == len(target.elts):
                for element, item in zip(target.elts, value.elts, strict=True):
                    self.bind(element, item, state)
            else:
                for element in target.elts:
                    self.bind(element, value, state)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, value, state)
        elif isinstance(target, ast.Name):
            alias = self.alias(value, state) if value is not None else None
            if alias is None:
                state.pop(target.id, None)
            else:
                state[target.id] = alias

    def store(self, target: ast.expr, state: _State) -> None:
        if isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                self.store(element, state)
            return
        if not isinstance(target, ast.Attribute | ast.Subscript):
            return
        root, links = _chain(target)
        alias = state.get(root) if root is not None else None
        if alias is not None and alias.written_by(links):
            self.found[id(target)] = target

    def block(self, body: list[ast.stmt], state: _State) -> _State:
        state = dict(state)
        for statement in body:
            state = self.statement(statement, state)
        return state

    def loop(self, body: list[ast.stmt], orelse: list[ast.stmt], entry: _State) -> _State:
        """A loop body read twice, so a name rebound late in one pass is seen by the next."""
        once = self.block(body, entry)
        twice = self.block(body, _merge(entry, once))
        return self.block(orelse, _merge(entry, once, twice))

    def statement(self, node: ast.stmt, state: _State) -> _State:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            params = {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
            inner = {name: alias for name, alias in state.items() if name not in params}
            inner.update(dict.fromkeys(self.inputs(node), _Alias()))
            self.block(node.body, inner)
            state.pop(node.name, None)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                self.store(target, state)
            for target in node.targets:
                self.bind(target, node.value, state)
        elif isinstance(node, ast.AnnAssign):
            self.store(node.target, state)
            if node.value is not None:
                self.bind(node.target, node.value, state)
        elif isinstance(node, ast.AugAssign):
            self.store(node.target, state)
        elif isinstance(node, ast.For | ast.AsyncFor):
            entry = dict(state)
            alias = self.alias(node.iter, state)
            for name in _target_names(node.target):
                if alias is None:
                    entry.pop(name, None)
                else:
                    entry[name] = _Alias()
            return self.loop(node.body, node.orelse, entry)
        elif isinstance(node, ast.While):
            return self.loop(node.body, node.orelse, state)
        elif isinstance(node, ast.If):
            return _merge(self.block(node.body, state), self.block(node.orelse, state))
        elif isinstance(node, ast.With | ast.AsyncWith):
            return self.block(node.body, state)
        elif isinstance(node, ast.Try | ast.TryStar):
            tried = self.block(node.body, state)
            handled = [self.block(h.body, _merge(state, tried)) for h in node.handlers]
            done = _merge(self.block(node.orelse, tried), *handled)
            return self.block(node.finalbody, done)
        elif isinstance(node, ast.Match):
            return _merge(*(self.block(case.body, state) for case in node.cases), state)
        return state


def _nan_checks(scope: FunctionNode) -> set[int]:
    """``id()`` of each ``isinstance`` call that guards ``isnan``: ``isinstance(v, float) and math.isnan(v)``.

    An unscored value is a float NaN under every reducer, so that check is safe.
    """
    guards: set[int] = set()
    for node in ast.walk(scope):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And)):
            continue
        calls = [operand for operand in node.values if isinstance(operand, ast.Call)]
        if any(get_call_name(call) == "isnan" for call in calls):
            guards.update(id(call) for call in calls if get_call_name(call) == "isinstance")
    return guards


def _single_type(node: ast.expr) -> str | None:
    """``int`` or ``float`` when an ``isinstance`` second argument is exactly that one type."""
    if isinstance(node, ast.Name) and node.id in ("int", "float"):
        return node.id
    return None


_NARROWING_HINT = (
    "convert the value with value_to_float(), as accuracy() and mean() do, and compare "
    'the float to a threshold; it leaves floats unchanged and maps "C", "I", "P", '
    '"N", yes/no and bools, where Score.as_float() raises on "C". Declare '
    '@metric(scores="unreduced") if the metric needs each epoch\'s own value'
)
_ISINSTANCE_HINT = (
    "drop the type filter and convert each value with value_to_float(): under mean the "
    "filter does nothing, and under mode or max it drops samples. A guard that raises "
    "because the task pins its reducer can stay, suppressed with a reason"
)
_DROPPED_HINT = (
    "put what the metric needs in Score.value, which every reducer combines, or declare "
    '@metric(scores="unreduced")'
)
_MUTATION_HINT = (
    "build new objects instead of writing to the ones passed in: "
    "sample_score.model_copy(update=...) to replace a field, or model_copy(deep=True) "
    "before changing one in place"
)


def _scope_findings(scope: _Scope) -> Iterator[tuple[ast.AST, str, Severity, str]]:
    """``(node, message, severity, hint)`` for each problem in one decorated function."""
    if scope.reduced:
        values = _Values(scope.node)
        nan_checks = _nan_checks(scope.node)
        for node in ast.walk(scope.node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _NARROWING_METHODS:
                    yield (
                        node,
                        f".{node.func.attr}() in a metric narrows an epoch-averaged score",
                        "error",
                        _NARROWING_HINT,
                    )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "int" and node.args and values.reads_value(node.args[0]):
                    yield (
                        node,
                        "int() on a score value in a metric truncates an epoch-averaged score",
                        "error",
                        _NARROWING_HINT,
                    )
                elif (
                    node.func.id == "isinstance"
                    and len(node.args) == 2
                    and (single := _single_type(node.args[1])) is not None
                    and values.is_value(node.args[0])
                    and id(node) not in nan_checks
                ):
                    yield (
                        node,
                        f"isinstance(..., {single}) on a score value holds only for some epoch reducers",
                        "warning",
                        _ISINSTANCE_HINT,
                    )
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in _DROPPED_FIELDS
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "score"
            ):
                yield (
                    node,
                    f"metric reads Score.{node.attr}, which the epoch reducer sets to None "
                    "unless every epoch agrees",
                    "error",
                    _DROPPED_HINT,
                )
    for target in _Writes(scope.node).found.values():
        yield (
            target,
            f"{scope.kind} writes to the scores it was handed: {ast.unparse(target)}",
            "error",
            _MUTATION_HINT,
        )


@rule(
    code="IEBP014",
    name="metric_epoch_safety",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Custom metrics read scores as the epoch reducer leaves them and do not modify them",
    references=(
        inspect_docs("metrics", "Metrics: Reducing Epochs", "reducing-epochs"),
        inspect_docs("metrics", "Metrics: Metrics and Reducers", "metrics-and-reducers"),
    ),
)
def metric_epoch_safety(ctx: LintContext) -> Iterable[Finding]:
    """Custom metrics read scores as the epoch reducer leaves them and do not modify them.

    ## What it does
    Reads every ``@metric`` function, including the function it returns and any
    helper nested in it, and every ``@score_reducer`` function. Three things are
    flagged, one diagnostic per site:

    - Narrowing a score value in a metric (error): ``.as_int()``, ``.as_bool()``,
      or ``int(...)`` of an expression computed from ``.value``, such as
      ``int(value["count"])`` or ``int(sum(values))``. ``int(len(...))`` and
      ``int()`` of a comparison are not flagged. ``isinstance(v, int)`` or
      ``isinstance(v, float)`` applied directly to a score value is a warning;
      ``isinstance(v, (int, float))`` and the NaN check
      ``isinstance(v, float) and math.isnan(v)`` are not flagged.
    - Reading ``<x>.score.answer``, ``.explanation`` or ``.reason`` in a metric
      (error).
    - Writing to the scores a metric or reducer was handed (error): an attribute
      or subscript assignment rooted at its scores parameter, at a loop variable
      over it, or at a name bound to either. The scores parameter is the first
      parameter of the function a ``@metric`` or ``@score_reducer`` factory
      returns, or any nested parameter annotated ``SampleScore`` or ``Score``.
      Names are followed in statement order, so rebinding one to a new object,
      such as ``s = s.model_copy(deep=True)`` or ``copy.deepcopy(s)``, ends it.
      A shallow ``model_copy()`` or ``copy.copy()`` shares the objects it holds,
      so ``c.score.value = 0`` on one is flagged and ``c.score = ...`` is not.

    A metric declared ``@metric(scores="unreduced")`` receives each epoch's own
    score, so only the third check applies to it. A plain function that a
    metric calls is not read.

    ## Why is this bad?
    By default, Inspect runs the epoch reducer before every metric, even at
    ``epochs=1`` (``_reduced_score`` in ``inspect_ai/scorer/_reducer/reducer.py``);
    ``@metric(scores="unreduced")`` and ``--no-epochs-reducer`` skip it. The score
    a metric receives is a new one, and its ``value`` is whatever the reducer
    made of the epochs, so a metric cannot assume the shape the scorer wrote.
    The default ``mean``, and ``median``, ``pass_at`` and ``pass_k``, compute a
    float through ``value_to_float``, so under ``mean`` it can be a fraction
    such as 0.667. ``at_least`` gives 1 or 0. ``mode``, ``majority`` and ``max``
    keep a raw value, such as ``"C"``, and ``collect`` gives a list of them. ``answer``, ``explanation`` and ``reason`` are kept only when they
    are equal across all epochs, and are otherwise ``None``. ``metadata`` comes
    from the first epoch.

    So ``int()`` and ``.as_int()`` round a sample that passed two epochs out of
    three down to 0, ``.as_bool()`` rounds it up to ``True``, and a single-type
    ``isinstance`` check is true or false depending on which reducer the run
    used. A type filter does nothing under ``mean`` and drops samples under
    ``mode`` or ``max``. The conversion that holds under every reducer is
    ``value_to_float()``, which ``accuracy()`` and ``mean()`` use: it leaves a
    float unchanged and maps ``"C"`` to 1.0, where ``Score.as_float()`` raises.
    A metric that counts ``score.answer == "win"`` counts a sample that won
    one epoch and lost another as neither. All of these pass a single-epoch test
    and go wrong with ``--epochs``. Writing to the scores is a different hazard:
    Inspect hands the same score objects to every metric on a scorer, and
    ``grouped()`` hands them to its inner metric more than once, so a later
    metric sees the changed values.

    ## Example
    ```python
    @metric
    def win_rate() -> Metric:
        def metric(scores: list[SampleScore]) -> float:
            return sum(s.score.answer == "win" for s in scores) / len(scores)

        return metric
    ```
    Use instead: give the scorer a value every reducer can average, and read it
    as a float.
    ```python
    # scorer: Score(value={"win": 1.0 if won else 0.0}, answer=outcome)
    @metric
    def win_rate() -> Metric:
        def metric(scores: list[SampleScore]) -> float:
            return sum(s.score.as_dict()["win"] for s in scores) / len(scores)

        return metric
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    total = 0
    issues: list[Diagnostic] = []
    for parsed in parsed_files.parsed:
        seen: set[int] = set()
        for scope in _decorated_scopes(parsed.tree):
            total += 1
            for node, message, severity, hint in _scope_findings(scope):
                if id(node) in seen:
                    continue
                seen.add(id(node))
                issues.append(
                    Diagnostic(
                        message,
                        file=parsed.path,
                        line=getattr(node, "lineno", None),
                        column=column_of(node),
                        end_line=end_line_of(node),
                        severity=severity,
                        hint=hint,
                    )
                )

    # ast.walk is breadth-first, so sort to report sites in source order.
    yield from sorted(issues, key=lambda d: (str(d.file), d.line or 0, d.column or 0))
    if issues:
        return
    if total == 0:
        yield Outcome("skip", "No @metric or @score_reducer functions found")
    else:
        yield Outcome(
            "pass", f"All {total} custom metric(s) and reducer(s) read reduced scores safely"
        )
