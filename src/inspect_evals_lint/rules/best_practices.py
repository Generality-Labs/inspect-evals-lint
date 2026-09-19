"""Best-practice rules: late model resolution, deliberate model roles, stable sample IDs, overridable task parameters."""

from __future__ import annotations

import ast
from collections.abc import Iterable

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import (
    column_of,
    get_call_name,
    get_decorator_name,
    parse_failures,
    parse_python_files,
)


class GetModelVisitor(ast.NodeVisitor):
    """Record every ``get_model()`` call with whether it sits inside a ``@solver``/``@scorer``."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []  # (line, context, is_valid)
        self.nodes: list[ast.Call] = []
        self._in_solver_or_scorer = False
        self._current_context = "module"

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        old_context = self._current_context
        old_in_solver_or_scorer = self._in_solver_or_scorer

        self._current_context = node.name
        for decorator in node.decorator_list:
            if get_decorator_name(decorator) in ("solver", "scorer"):
                self._in_solver_or_scorer = True
                break

        self.generic_visit(node)

        self._current_context = old_context
        self._in_solver_or_scorer = old_in_solver_or_scorer

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:
        if get_call_name(node) == "get_model":
            self.calls.append((node.lineno, self._current_context, self._in_solver_or_scorer))
            self.nodes.append(node)
        self.generic_visit(node)


@rule(
    code="IEBP001",
    name="get_model_location",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="get_model() is only called inside @solver or @scorer functions",
)
def get_model_location(ctx: LintContext) -> Iterable[Finding]:
    """``get_model()`` is only called inside ``@solver`` or ``@scorer`` functions.

    ## What it does
    Warns on each ``get_model()`` call that is not inside a function decorated with
    ``@solver`` or ``@scorer``.

    ## Why is this bad?
    Resolving a concrete model at import time or inside ``@task`` fixes it before
    the caller can choose one. Resolving it inside the solver or scorer keeps the
    task declarative and lets ``--model-role`` and task parameters override it.

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
                f"get_model() called outside @solver/@scorer (in {context})",
                file=parsed.path,
                line=line,
                column=column_of(node),
                severity="warning",
                hint="resolve models inside @solver/@scorer so tasks stay declarative",
            )
    if not found:
        yield Outcome(
            "pass", "get_model() calls are properly inside @solver/@scorer decorated functions"
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


@rule(
    code="IEBP003",
    name="sample_ids",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Every Sample() passes id=",
)
def sample_ids(ctx: LintContext) -> Iterable[Finding]:
    """Every ``Sample()`` passes ``id=``.

    ## What it does
    Flags each ``Sample(...)`` call without an ``id=`` keyword.

    ## Why is this bad?
    Without a stable id a sample is identified by its position. Shuffling,
    ``--limit``, reruns and dataset updates all change positions, so results can
    no longer be compared sample by sample.

    ## Example
    ```python
    Sample(input=record["question"], target=record["answer"])
    ```
    Use instead:
    ```python
    Sample(input=record["question"], target=record["answer"], id=record["id"])
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    total = 0
    missing = 0
    for parsed in parsed_files.parsed:
        visitor = SampleIdVisitor()
        visitor.visit(parsed.tree)
        for (line, has_id), node in zip(visitor.samples, visitor.nodes, strict=True):
            total += 1
            if has_id:
                continue
            missing += 1
            yield Diagnostic(
                "Sample() call without id=",
                file=parsed.path,
                line=line,
                column=column_of(node),
                hint="pass a stable id= so the sample survives shuffles and reruns",
            )

    if total == 0:
        yield Outcome("skip", "No Sample() calls found")
    elif missing == 0:
        yield Outcome("pass", f"All {total} Sample() calls include id parameter")


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
                hint=_MODEL_ROLE_HINT,
                key=role,
            )

    if issues:
        return
    if total_role_calls == 0:
        yield Outcome("skip", "No get_model(role=...) calls found")
    else:
        yield Outcome("pass", f"All {total_role_calls} model role call(s) resolve deliberately")
