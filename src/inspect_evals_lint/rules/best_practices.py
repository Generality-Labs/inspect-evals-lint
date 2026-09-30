"""Best-practice rules: late model resolution, deliberate model roles, stable sample IDs, overridable task parameters."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, cast

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import (
    column_of,
    end_line_of,
    get_call_name,
    get_decorator_name,
    parse_failures,
    parse_python_files,
)

MODEL_RESOLVING_DECORATORS = ("solver", "scorer", "agent")
"""The components inside which ``get_model()`` resolves late enough for the caller to override it."""


class GetModelVisitor(ast.NodeVisitor):
    """Record every ``get_model()`` call with whether it sits inside a ``@solver``, ``@scorer`` or ``@agent``."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []  # (line, context, is_valid)
        self.nodes: list[ast.Call] = []
        self._in_component = False
        self._current_context = "module"

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        old_context = self._current_context
        old_in_component = self._in_component

        self._current_context = node.name
        for decorator in node.decorator_list:
            if get_decorator_name(decorator) in MODEL_RESOLVING_DECORATORS:
                self._in_component = True
                break

        self.generic_visit(node)

        self._current_context = old_context
        self._in_component = old_in_component

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:
        if get_call_name(node) == "get_model":
            self.calls.append((node.lineno, self._current_context, self._in_component))
            self.nodes.append(node)
        self.generic_visit(node)


@rule(
    code="IEBP001",
    name="get_model_location",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="get_model() is only called inside @solver, @scorer or @agent functions",
    references=(
        inspect_docs("models", "Models: Role Resolution", "role-resolution"),
        inspect_docs("solvers", "Solvers: Models in Solvers", "models-in-solvers"),
        inspect_docs("custom-scorers", "Custom Scorers: Models in Scorers", "models-in-scorers"),
        inspect_docs("agent-custom", "Custom Agents: Parameters", "parameters"),
    ),
)
def get_model_location(ctx: LintContext) -> Iterable[Finding]:
    """``get_model()`` is only called inside ``@solver``, ``@scorer`` or ``@agent`` functions.

    ## What it does
    Warns on each ``get_model()`` call that is not inside a function decorated with
    ``@solver``, ``@scorer`` or ``@agent``. An agent is the solver of a sandboxed
    evaluation, and its ``execute`` runs per sample just as a solver's does.

    ## Why is this bad?
    Resolving a concrete model at import time or inside ``@task`` fixes it before
    the caller can choose one. Resolving it inside the solver, scorer or agent keeps
    the task declarative and lets ``--model-role`` and task parameters override it.

    ## Example
    ```python
    GRADER = get_model("openai/gpt-4o")   # resolved at import

    @scorer(metrics=[accuracy()])
    def graded():
        async def score(state, target):
            return await GRADER.generate(...)
    ```
    Use instead:
    ```python
    @scorer(metrics=[accuracy()])
    def graded(model: str | Model | None = None):
        async def score(state, target):
            grader = get_model(model, role="grader", default="openai/gpt-4o")
            ...
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    found = False
    for parsed in parsed_files.parsed:
        visitor = GetModelVisitor()
        visitor.visit(parsed.tree)
        for (line, context, is_valid), node in zip(visitor.calls, visitor.nodes, strict=True):
            if is_valid:
                continue
            found = True
            yield Diagnostic(
                f"get_model() called outside @solver/@scorer/@agent (in {context})",
                file=parsed.path,
                line=line,
                column=column_of(node),
                end_line=end_line_of(node),
                severity="warning",
                hint="resolve models inside @solver/@scorer/@agent so tasks stay declarative",
            )
    if not found:
        yield Outcome(
            "pass",
            "get_model() calls are properly inside @solver/@scorer/@agent decorated functions",
        )


class TaskParameterVisitor(ast.NodeVisitor):
    """Record ``@task`` functions with their parameter names."""

    def __init__(self) -> None:
        self.tasks: list[tuple[str, int, set[str]]] = []  # (name, line, params)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if any(get_decorator_name(d) == "task" for d in node.decorator_list):
            params = {arg.arg for arg in node.args.args}
            params.update(arg.arg for arg in node.args.kwonlyargs)
            self.tasks.append((node.name, node.lineno, params))
        self.generic_visit(node)


class SampleIdVisitor(ast.NodeVisitor):
    """Record ``Sample(...)`` calls with whether they pass ``id=``."""

    def __init__(self) -> None:
        self.samples: list[tuple[int, bool]] = []  # (line, has_id)
        self.nodes: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        if get_call_name(node) == "Sample":
            has_id = any(keyword.arg == "id" for keyword in node.keywords)
            self.samples.append((node.lineno, has_id))
            self.nodes.append(node)
        self.generic_visit(node)


FIELD_SPEC_ID_POSITION = 3
"""``FieldSpec(input, target, choices, id, ...)``: a fourth positional argument is the id field."""


@dataclass
class FieldSpecSite:
    """A ``FieldSpec(...)`` call, whether it names an id field, and the loader calls it is passed to."""

    node: ast.Call
    has_id: bool
    loaders: dict[int, ast.Call] = field(default_factory=dict)
    """Calls passing this FieldSpec as ``sample_fields=``, directly or through a name bound to it."""

    @property
    def auto_id(self) -> bool:
        """Whether every loader it is passed to numbers its samples with ``auto_id=``."""
        return bool(self.loaders) and all(_passes_auto_id(call) for call in self.loaders.values())


def _is_field_spec(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Call) and get_call_name(node) == "FieldSpec"


def _passes_auto_id(call: ast.Call) -> bool:
    """``auto_id=`` with anything but a false literal; a non-literal is taken as set, as ``required=`` is."""
    keyword = next((k for k in call.keywords if k.arg == "auto_id"), None)
    if keyword is None:
        return False
    return not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)


def _field_spec_has_id(node: ast.Call) -> bool:
    """``id=``, a fourth positional argument, or ``**kwargs`` (which may hold one)."""
    return len(node.args) > FIELD_SPEC_ID_POSITION or any(
        keyword.arg in ("id", None) for keyword in node.keywords
    )


Position = tuple[int, int]
"""``(line, column)`` in the file, for ordering bindings against the calls that use them."""


@dataclass
class _Binding:
    """One binding of a name: the value bound, where it takes effect, and the statement lists it sits in."""

    value: ast.expr | None
    """None where the value isn't an expression in the file: a parameter, an import, a loop target."""
    position: Position
    blocks: tuple[int, ...]
    """``id`` of each statement list enclosing the binding, outermost first."""


@dataclass
class _Scope:
    """A module, function, class or comprehension body, with the names it binds."""

    kind: Literal["module", "function", "class", "comprehension"]
    parent: _Scope | None
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)
    declared: dict[str, Literal["global", "nonlocal"]] = field(default_factory=dict)


@dataclass
class _LoaderCall:
    """A call passing ``sample_fields``, with where it runs."""

    call: ast.Call
    sample_fields: ast.expr
    scope: _Scope
    blocks: tuple[int, ...]


def _position(node: ast.expr | ast.stmt | ast.excepthandler | ast.pattern) -> Position:
    return (node.lineno, node.col_offset)


def _end(node: ast.expr | ast.stmt) -> Position:
    return (node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)


def _unpack(target: ast.expr, value: ast.expr | None) -> Iterable[tuple[ast.Name, ast.expr | None]]:
    """The names an assignment target binds, each with its part of ``value`` where that can be told."""
    if isinstance(target, ast.Name):
        yield target, value
    elif isinstance(target, ast.Starred):
        yield from _unpack(target.value, None)
    elif isinstance(target, (ast.Tuple, ast.List)):
        parts: list[ast.expr | None] = [None] * len(target.elts)
        if (
            isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(target.elts)
            and not any(isinstance(e, ast.Starred) for e in [*target.elts, *value.elts])
        ):
            parts = list(value.elts)
        for element, part in zip(target.elts, parts, strict=True):
            yield from _unpack(element, part)


class _BindingCollector(ast.NodeVisitor):
    """Record each scope's name bindings and each call passing ``sample_fields``, in one pass over a file."""

    def __init__(self) -> None:
        self.module = _Scope("module", None)
        self.scope: _Scope = self.module
        self.blocks: list[int] = []
        self.loaders: list[_LoaderCall] = []
        self._bound: set[int] = set()
        """``id`` of each ``Name`` target already bound with its value."""

    def _bind(self, name: str, value: ast.expr | None, position: Position) -> None:
        scope = self.scope
        while scope.kind == "comprehension" and scope.parent is not None and value is not None:
            scope = scope.parent  # only a walrus binds a value inside a comprehension
        declared = scope.declared.get(name)
        if declared == "global":
            scope = self.module
        elif declared == "nonlocal":
            enclosing = scope.parent
            while enclosing is not None and enclosing.kind != "function":
                enclosing = enclosing.parent
            scope = enclosing or scope
        scope.bindings.setdefault(name, []).append(_Binding(value, position, tuple(self.blocks)))

    def generic_visit(self, node: ast.AST) -> None:
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                self._visit_list(cast(list[object], value))
            elif isinstance(value, ast.AST):
                self.visit(value)

    def _visit_list(self, items: list[object]) -> None:
        block = bool(items) and isinstance(items[0], ast.stmt)
        if block:
            self.blocks.append(id(items))
        for item in items:
            if isinstance(item, ast.AST):
                self.visit(item)
        if block:
            self.blocks.pop()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store) and id(node) not in self._bound:
            self._bind(node.id, None, _position(node))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            for name, value in _unpack(target, node.value):
                self._bound.add(id(name))
                self._bind(name.id, value, _end(node))
            self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is None:
            return
        self.visit(node.value)
        for name, value in _unpack(node.target, node.value):
            self._bound.add(id(name))
            self._bind(name.id, value, _end(node))
        self.visit(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bound.add(id(node.target))
        self._bind(node.target.id, node.value, _end(node))

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.declared.update(dict.fromkeys(node.names, "global"))

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.declared.update(dict.fromkeys(node.names, "nonlocal"))

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            self._bind(alias.asname or alias.name.split(".")[0], None, _position(node))

    visit_ImportFrom = visit_Import  # noqa: N815

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, None, _position(node))
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs | ast.MatchStar) -> None:
        if node.name:
            self._bind(node.name, None, _position(node))
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs  # noqa: N815

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        arguments = node.args
        # Decorators and defaults run in the enclosing scope.
        decorators = [] if isinstance(node, ast.Lambda) else node.decorator_list
        for expr in [*decorators, *arguments.defaults, *arguments.kw_defaults]:
            if expr is not None:
                self.visit(expr)
        outer = self.scope
        self.scope = _Scope("function", outer)
        parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        parameters += [a for a in (arguments.vararg, arguments.kwarg) if a is not None]
        for parameter in parameters:
            self._bind(parameter.arg, None, _position(node))
        if isinstance(node, ast.Lambda):
            self.visit(node.body)
        else:
            self._visit_list(list(node.body))
        self.scope = outer
        if not isinstance(node, ast.Lambda):
            self._bind(node.name, None, _end(node))

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815
    visit_Lambda = visit_FunctionDef  # noqa: N815

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]:
            self.visit(expr)
        outer = self.scope
        self.scope = _Scope("class", outer)
        self._visit_list(list(node.body))
        self.scope = outer
        self._bind(node.name, None, _end(node))

    def visit_ListComp(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        outer = self.scope
        self.scope = _Scope("comprehension", outer)
        self.generic_visit(node)
        self.scope = outer

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:
        value = _sample_fields_argument(node)
        if value is not None:
            self.loaders.append(_LoaderCall(node, value, self.scope, tuple(self.blocks)))
        self.generic_visit(node)


def _sample_fields_argument(call: ast.Call) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == "sample_fields"), None)


def _within(outer: tuple[int, ...], inner: tuple[int, ...]) -> bool:
    """Whether the statement list at the end of ``outer`` is, or encloses, the one at the end of ``inner``."""
    return inner[: len(outer)] == outer


def _defining_scope(name: str, scope: _Scope) -> tuple[_Scope, bool] | None:
    """The scope whose bindings of ``name`` a use in ``scope`` sees, and whether that scope's code runs in order up to the use.

    A function body runs after its enclosing code, so any binding there may be the one in force.
    A class body is skipped once left, as Python does for the functions it defines.
    """
    in_order = True
    current: _Scope | None = scope
    while current is not None:
        declared = current.declared.get(name)
        if declared == "global":
            while current.parent is not None:
                current = current.parent
            return (current, False) if name in current.bindings else None
        visible = current is scope or current.kind != "class"
        if declared is None and visible and name in current.bindings:
            return current, in_order
        if current.kind == "function":
            in_order = False
        current = current.parent
    return None


def _reaching(
    name: str, scope: _Scope, position: Position, blocks: tuple[int, ...]
) -> list[_Binding]:
    """The bindings of ``name`` that may be in force at ``position`` in ``scope``.

    Where the defining scope runs in order up to the use, a binding is in force
    if it comes earlier and no later binding in the same or an enclosing
    statement list, one that also encloses the use, replaces it.
    """
    found = _defining_scope(name, scope)
    if found is None:
        return []
    owner, in_order = found
    bindings = owner.bindings[name]
    if not in_order:
        return bindings
    earlier = [b for b in bindings if b.position < position]
    return [
        b
        for b in earlier
        if not any(
            later.position > b.position
            and _within(later.blocks, b.blocks)
            and _within(later.blocks, blocks)
            for later in earlier
        )
    ]


def _field_specs_in(
    expr: ast.expr, scope: _Scope, position: Position, blocks: tuple[int, ...], seen: frozenset[int]
) -> Iterable[ast.Call]:
    """The ``FieldSpec(...)`` calls ``expr`` may evaluate to: the call itself, or what a name is bound to."""
    if _is_field_spec(expr):
        yield cast(ast.Call, expr)
    elif isinstance(expr, ast.Name):
        for binding in _reaching(expr.id, scope, position, blocks):
            if binding.value is not None and id(binding) not in seen:
                yield from _field_specs_in(
                    binding.value, scope, binding.position, binding.blocks, seen | {id(binding)}
                )


def field_spec_sites(tree: ast.AST) -> list[FieldSpecSite]:
    """Every ``FieldSpec(...)`` call in a file, with the loaders it reaches.

    A loader is any call passing ``sample_fields=``. The value may be the
    ``FieldSpec(...)`` call itself or a name bound to one, resolved as Python
    scopes it: a function's own names, then enclosing functions', then the
    module's. A function passed as ``sample_fields`` builds its own
    ``Sample()`` calls, which are checked as such.
    """
    sites = {
        id(node): FieldSpecSite(node, _field_spec_has_id(node))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_field_spec(node)
    }
    collector = _BindingCollector()
    collector.visit(tree)
    for loader in collector.loaders:
        for spec in _field_specs_in(
            loader.sample_fields, loader.scope, _position(loader.call), loader.blocks, frozenset()
        ):
            sites[id(spec)].loaders[id(loader.call)] = loader.call
    return sorted(sites.values(), key=lambda site: (site.node.lineno, site.node.col_offset))


_FIELD_SPEC_HINT = (
    'name the record\'s id field with id= (FieldSpec reads a field called "id" by default, so '
    'write id="id" if that is the one), or pass auto_id=True to the loader'
)


@rule(
    code="IEBP003",
    name="sample_ids",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Every Sample() passes id=, and every FieldSpec() names an id field",
    references=(
        inspect_docs("datasets", "Datasets: Dataset Samples", "dataset-samples"),
        inspect_docs("eval-logs", "Log Files: IDs and Shuffling", "ids-and-shuffling"),
    ),
)
def sample_ids(ctx: LintContext) -> Iterable[Finding]:
    """Every ``Sample()`` passes ``id=``, and every ``FieldSpec()`` names an id field.

    ## What it does
    Flags each ``Sample(...)`` call without an ``id=`` keyword.

    Also flags each ``FieldSpec(...)`` without ``id=`` (or a fourth positional
    argument), unless every loader call it is passed to as ``sample_fields=``
    sets ``auto_id=True``. The ``FieldSpec`` may be passed directly or through
    a name bound to it in the same function or at module level; one bound but
    never passed to a loader needs its own ``id=``. ``FieldSpec`` reads a field
    called ``id`` by default, but whether the records have one is not visible
    in the code, so the rule asks for the field to be named: ``id="id"`` when
    that is it. A function passed as ``sample_fields`` builds its own
    ``Sample()`` calls, which are checked as above.

    ``auto_id`` numbers samples by their position in the unshuffled records.
    Inspect assigns those ids before shuffling, so they survive shuffles, but
    not filtering or dataset updates; a field from the records is more stable
    where one exists.

    ## Why is this bad?
    Without a stable id a sample is identified by its position. Shuffling,
    ``--limit``, reruns and dataset updates all change positions, so results can
    no longer be compared sample by sample.

    ## Example
    ```python
    Sample(input=record["question"], target=record["answer"])
    hf_dataset("org/data", sample_fields=FieldSpec(input="question", target="answer"))
    ```
    Use instead:
    ```python
    Sample(input=record["question"], target=record["answer"], id=record["id"])
    hf_dataset("org/data", sample_fields=FieldSpec(input="question", target="answer", id="qid"))
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    samples = 0
    specs = 0
    missing = 0
    for parsed in parsed_files.parsed:
        visitor = SampleIdVisitor()
        visitor.visit(parsed.tree)
        found: list[tuple[ast.Call, str, str]] = []
        for (_line, has_id), node in zip(visitor.samples, visitor.nodes, strict=True):
            samples += 1
            if not has_id:
                found.append(
                    (
                        node,
                        "Sample() call without id=",
                        "pass a stable id= so the sample survives shuffles and reruns",
                    )
                )
        for site in field_spec_sites(parsed.tree):
            specs += 1
            if not site.has_id and not site.auto_id:
                found.append(
                    (
                        site.node,
                        "FieldSpec() without id=, and its loader does not pass auto_id=True",
                        _FIELD_SPEC_HINT,
                    )
                )
        for node, message, hint in sorted(found, key=lambda f: (f[0].lineno, f[0].col_offset)):
            missing += 1
            yield Diagnostic(
                message,
                file=parsed.path,
                line=node.lineno,
                column=column_of(node),
                end_line=end_line_of(node),
                hint=hint,
            )

    if samples + specs == 0:
        yield Outcome("skip", "No Sample() or FieldSpec() calls found")
    elif missing == 0:
        yield Outcome("pass", f"All {samples} Sample() and {specs} FieldSpec() calls give an id")


class TaskDefaultsVisitor(ast.NodeVisitor):
    """Record ``@task`` functions with, per parameter, whether it has a default."""

    def __init__(self) -> None:
        self.tasks: list[tuple[str, int, dict[str, bool]]] = []  # (name, line, param_defaults)
        self.nodes: list[ast.FunctionDef] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if any(get_decorator_name(d) == "task" for d in node.decorator_list):
            param_defaults: dict[str, bool] = {}

            num_defaults = len(node.args.defaults)
            args_with_defaults = (
                node.args.args[len(node.args.args) - num_defaults :] if num_defaults else []
            )
            for arg in node.args.args:
                param_defaults[arg.arg] = arg in args_with_defaults

            for i, arg in enumerate(node.args.kwonlyargs):
                param_defaults[arg.arg] = node.args.kw_defaults[i] is not None

            self.tasks.append((node.name, node.lineno, param_defaults))
            self.nodes.append(node)
        self.generic_visit(node)


OVERRIDABLE_PARAMS = {"solver", "scorer", "metric", "metrics", "grader", "model"}


@rule(
    code="IEBP004",
    name="task_overridable_defaults",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="@task parameters naming a solver, scorer, metric, grader or model have defaults",
    references=(
        inspect_docs("tasks", "Tasks: Parameters", "parameters"),
        inspect_docs("tasks", "Tasks: Solver Parameter", "solver-parameter"),
        inspect_docs("tasks", "Tasks: Scorer Override", "scorer-override"),
        inspect_docs("extensions-components", "Extensions: Components: Tasks", "tasks"),
    ),
)
def task_overridable_defaults(ctx: LintContext) -> Iterable[Finding]:
    """``@task`` parameters naming a solver, scorer, metric, grader or model have defaults.

    ## What it does
    Flags each parameter of a ``@task`` function whose name contains ``solver``,
    ``scorer``, ``metric``, ``metrics``, ``grader`` or ``model`` and has no
    default.

    ## Why is this bad?
    These are the pieces callers most often want to swap. With defaults the task
    runs unconfigured, ``inspect eval my_eval/task`` just works, and each piece can
    still be overridden with ``-T``.

    ## Example
    ```python
    @task
    def my_eval(solver, grader_model):
        ...
    ```
    Use instead:
    ```python
    @task
    def my_eval(solver: Solver | None = None, grader_model: str | None = None):
        ...
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    total_tasks = 0
    found = False
    for parsed in parsed_files.parsed:
        visitor = TaskDefaultsVisitor()
        visitor.visit(parsed.tree)
        for (task_name, line, param_defaults), node in zip(
            visitor.tasks, visitor.nodes, strict=True
        ):
            total_tasks += 1
            for param, has_default in param_defaults.items():
                if has_default or not any(key in param.lower() for key in OVERRIDABLE_PARAMS):
                    continue
                found = True
                yield Diagnostic(
                    f"@task {task_name}() parameter {param!r} has no default",
                    file=parsed.path,
                    line=line,
                    column=column_of(node),
                    hint="give overridable parameters a default so the task runs unconfigured",
                )

    if total_tasks == 0:
        yield Outcome("skip", "No @task decorated functions found")
    elif not found:
        yield Outcome("pass", "Tasks provide defaults for overridable parameters")


class ModelRoleVisitor(ast.NodeVisitor):
    """Record every ``get_model(role=...)`` call with whether the role resolves deliberately."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []  # (line, role, resolves_deliberately)
        self.nodes: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        if get_call_name(node) == "get_model":
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
            if "role" in keywords:
                self.calls.append(
                    (
                        node.lineno,
                        _literal_role_name(keywords["role"]),
                        _resolves_deliberately(node, keywords),
                    )
                )
                self.nodes.append(node)
        self.generic_visit(node)


def _literal_role_name(node: ast.expr) -> str:
    """Role name if given as a literal, else a placeholder for the message."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "<dynamic>"


def _is_literal_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _resolves_deliberately(node: ast.Call, keywords: dict[str, ast.expr]) -> bool:
    """Whether the role can resolve to anything but the model under evaluation.

    Any of three things suffices, matching ``get_model()``'s own precedence: an
    explicit model, a pinned ``default=``, or ``required=True``. The explicit
    model counts because ``get_model()`` only consults its default (and
    ultimately the model under evaluation) when ``model is None``.
    """
    return _has_model(node, keywords) or _has_default(keywords) or _is_required(keywords)


def _has_model(node: ast.Call, keywords: dict[str, ast.expr]) -> bool:
    """Whether an explicit model is supplied, positionally or by keyword."""
    if node.args and not _is_literal_none(node.args[0]):
        return True
    return "model" in keywords and not _is_literal_none(keywords["model"])


def _has_default(keywords: dict[str, ast.expr]) -> bool:
    """Whether a fallback model is pinned via ``default=``.

    A literal ``default=None`` leaves the fallback exactly where it was, so it
    must not count: otherwise a no-op edit could silence the check.
    """
    return "default" in keywords and not _is_literal_none(keywords["default"])


def _is_required(keywords: dict[str, ast.expr]) -> bool:
    """Whether the role is marked required.

    A non-literal ``required=`` (a variable) is taken at face value rather than
    flagged, since its value isn't knowable statically.
    """
    if "required" not in keywords:
        return False
    value = keywords["required"]
    if isinstance(value, ast.Constant):
        return bool(value.value)
    return True


_MODEL_ROLE_HINT = (
    "pass an explicit model, pin a default=, or mark it required=True; an unbound "
    "role otherwise falls back to the model under evaluation, so a grader can "
    "silently grade itself"
)


@rule(
    code="IEBP002",
    name="model_role_resolution",
    category="best_practices",
    scopes=("eval", "helper"),
    allowlist=True,
    summary="get_model(role=...) resolves deliberately: an explicit model, default= or required=True",
    references=(
        inspect_docs("models", "Models: Model Roles", "model-roles"),
        inspect_docs("models", "Models: Role Defaults", "role-defaults"),
        inspect_docs("tasks", "Tasks: Model Roles", "model-roles"),
    ),
)
def model_role_resolution(ctx: LintContext) -> Iterable[Finding]:
    """``get_model(role=...)`` resolves deliberately: an explicit model, ``default=`` or ``required=True``.

    ## What it does
    Flags each ``get_model(role=...)`` call that passes no explicit model, no
    ``default=`` and no ``required=True``. A literal ``default=None``,
    ``model=None`` or ``required=False`` changes nothing at runtime and does not
    count. Each diagnostic is keyed by the role name (``<dynamic>`` for a
    non-literal), which is what an allowlist entry names.

    ## Why is this bad?
    A role that is not bound at invocation falls back to the model under
    evaluation. A grader then grades the model's own output, and the scores still
    look plausible. Pinning a default or requiring the role makes the fallback a
    choice rather than an accident.

    ## Example
    ```python
    grader = get_model(role="grader")
    ```
    Use instead:
    ```python
    grader = get_model(role="grader", default="openai/gpt-4o")
    # or
    grader = get_model(role="grader", required=True)
    ```

    ## Options
    - `allowlists.model_role_resolution`: `{ package = ["role"] }` entries reported as warnings while an existing surface is burned down.
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    total_role_calls = 0
    issues = 0
    for parsed in parsed_files.parsed:
        visitor = ModelRoleVisitor()
        visitor.visit(parsed.tree)
        total_role_calls += len(visitor.calls)
        for (line, role, resolves), node in zip(visitor.calls, visitor.nodes, strict=True):
            if resolves:
                continue
            issues += 1
            yield Diagnostic(
                f"get_model(role={role!r}) has no model, no default= and no required=True",
                file=parsed.path,
                line=line,
                column=column_of(node),
                end_line=end_line_of(node),
                hint=_MODEL_ROLE_HINT,
                key=role,
            )

    if issues:
        return
    if total_role_calls == 0:
        yield Outcome("skip", "No get_model(role=...) calls found")
    else:
        yield Outcome("pass", f"All {total_role_calls} model role call(s) resolve deliberately")


def _string_literal(node: ast.expr) -> str | None:
    """The value of a string literal (implicit concatenation folds into one Constant), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _has_url(text: str) -> bool:
    return "http://" in text or "https://" in text


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword
    return None


def _has_star_kwargs(call: ast.Call) -> bool:
    return any(keyword.arg is None for keyword in call.keywords)


def _module_dict_literals(tree: ast.AST) -> dict[str, ast.Dict]:
    """Module-level ``NAME = {...}`` / ``NAME: T = {...}`` assignments, by name."""
    found: dict[str, ast.Dict] = {}
    for statement in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Dict):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and isinstance(statement.value, ast.Dict)
        ):
            found[statement.target.id] = statement.value
    return found


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and get_call_name(node) == name
    ]
    # ast.walk is breadth-first, so sort to report sites in source order.
    return sorted(calls, key=lambda node: (node.lineno, node.col_offset))


_DEDUP_HINT = (
    "measure the duplicates on the pinned dataset and pass max_duplicates=<n>, and give a "
    "reason= that links the upstream report; see BEST_PRACTICES.md on deduplicating by id"
)


@rule(
    code="IEBP008",
    name="duplicate_filter_acknowledged",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Every filter_duplicate_ids() call states how many duplicates it drops and links the upstream report",
    references=(
        inspect_docs("datasets", "Datasets: Dataset Samples", "dataset-samples"),
        inspect_docs("eval-logs", "Log Files: IDs and Shuffling", "ids-and-shuffling"),
    ),
)
def duplicate_filter_acknowledged(ctx: LintContext) -> Iterable[Finding]:
    """Every ``filter_duplicate_ids()`` call states how many duplicates it drops and links the upstream report.

    ## What it does
    Flags each ``filter_duplicate_ids(...)`` call that lacks a ``max_duplicates=``
    keyword, lacks a ``reason=`` keyword, or gives a literal ``reason`` with no
    ``http://`` or ``https://`` URL in it. A ``reason`` passed as a variable is
    taken at face value, as is ``**kwargs``. One diagnostic per call.

    ## Why is this bad?
    Dropping samples that share an id is only safe when they are the same record
    twice. A call with no count is a workaround nobody has measured, and a reason
    with no link is a defect nobody upstream knows about; both let a bad id key
    silently truncate the dataset, which is how WorldSense lost half its trials.
    The count bounds the damage a revision bump can do and the link is the
    evidence a reviewer can check.

    ## Example
    ```python
    dataset = filter_duplicate_ids(dataset)
    ```
    Use instead:
    ```python
    dataset = filter_duplicate_ids(
        dataset,
        max_duplicates=11,
        reason="8 groups of identical rows, see https://github.com/org/repo/issues/268",
    )
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    total = 0
    issues = 0
    for parsed in parsed_files.parsed:
        for call in _calls_named(parsed.tree, "filter_duplicate_ids"):
            total += 1
            if _has_star_kwargs(call):
                continue
            missing = [
                name for name in ("max_duplicates", "reason") if _keyword(call, name) is None
            ]
            if missing:
                message = f"filter_duplicate_ids() without {' or '.join(f'{m}=' for m in missing)}"
            else:
                reason = _keyword(call, "reason")
                assert reason is not None
                text = _string_literal(reason.value)
                if text is None or _has_url(text):
                    continue
                message = (
                    "filter_duplicate_ids() reason= does not link the upstream report (no URL)"
                )
            issues += 1
            yield Diagnostic(
                message,
                file=parsed.path,
                line=call.lineno,
                column=column_of(call),
                end_line=end_line_of(call),
                hint=_DEDUP_HINT,
            )

    if issues:
        return
    if total == 0:
        yield Outcome("skip", "No filter_duplicate_ids() calls found")
    else:
        yield Outcome(
            "pass", f"All {total} filter_duplicate_ids() call(s) state a count and link a report"
        )


_BROKEN_HINT = (
    "report the defect upstream and put the report URL as the entry's value; "
    "see BEST_PRACTICES.md on excluding known-broken samples"
)


@rule(
    code="IEBP009",
    name="known_broken_reported",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Every drop_known_broken() entry maps a sample id to the URL of its upstream report",
    references=(inspect_docs("datasets", "Datasets: Dataset Samples", "dataset-samples"),),
)
def known_broken_reported(ctx: LintContext) -> Iterable[Finding]:
    """Every ``drop_known_broken()`` entry maps a sample id to the URL of its upstream report.

    ## What it does
    Flags each ``drop_known_broken(...)`` call without a ``broken=`` keyword, and
    each entry of its ``broken`` dict whose value is a literal string with no
    ``http://`` or ``https://`` URL. The dict may be written inline or bound to a
    module-level name in the same file; a name the file does not define, a
    non-literal value and ``**kwargs`` are taken at face value. Entry diagnostics
    point at the entry's line so a suppression can sit beside it.

    ## Why is this bad?
    A hard-coded exclusion list is a workaround for a dataset defect. Without the
    report beside each id, nobody upstream knows about the defect, a reviewer
    cannot check the claim, and the entry outlives the fix. The URL is the
    evidence and the reminder.

    ## Example
    ```python
    KNOWN_BROKEN = {"ruin_names_100": "options split on commas"}
    ```
    Use instead:
    ```python
    KNOWN_BROKEN = {"ruin_names_100": "https://github.com/org/repo/issues/19"}
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    total = 0
    issues: list[Diagnostic] = []
    for parsed in parsed_files.parsed:
        constants = _module_dict_literals(parsed.tree)
        for call in _calls_named(parsed.tree, "drop_known_broken"):
            total += 1
            if _has_star_kwargs(call):
                continue
            broken = _keyword(call, "broken")
            if broken is None:
                issues.append(
                    Diagnostic(
                        "drop_known_broken() without broken=",
                        file=parsed.path,
                        line=call.lineno,
                        column=column_of(call),
                        end_line=end_line_of(call),
                        hint=_BROKEN_HINT,
                    )
                )
                continue
            value = broken.value
            if isinstance(value, ast.Name):
                literal = constants.get(value.id)
            elif isinstance(value, ast.Dict):
                literal = value
            else:
                literal = None
            if literal is None:
                continue
            for key, entry in zip(literal.keys, literal.values, strict=True):
                text = _string_literal(entry)
                if text is None or _has_url(text):
                    continue
                key_text = _string_literal(key) if key is not None else None
                label = repr(key_text) if key_text is not None else ast.unparse(key) if key else "?"
                issues.append(
                    Diagnostic(
                        f"known-broken entry {label} has no report URL",
                        file=parsed.path,
                        line=entry.lineno,
                        column=column_of(entry),
                        end_line=end_line_of(entry),
                        hint=_BROKEN_HINT,
                        key=label,
                    )
                )

    yield from sorted(issues, key=lambda d: (str(d.file), d.line or 0, d.column or 0))
    if issues:
        return
    if total == 0:
        yield Outcome("skip", "No drop_known_broken() calls found")
    else:
        yield Outcome("pass", f"All {total} drop_known_broken() call(s) link a report per entry")
