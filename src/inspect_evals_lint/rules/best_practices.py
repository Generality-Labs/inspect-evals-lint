"""Best-practice rules: late model resolution, deliberate model roles, stable sample IDs, overridable task parameters."""

from __future__ import annotations

import ast
from collections.abc import Iterable

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


@rule(
    code="IEBP003",
    name="sample_ids",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Every Sample() passes id=",
    references=(
        inspect_docs("datasets", "Datasets: Dataset Samples", "dataset-samples"),
        inspect_docs("eval-logs", "Log Files: IDs and Shuffling", "ids-and-shuffling"),
    ),
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
                end_line=end_line_of(node),
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
