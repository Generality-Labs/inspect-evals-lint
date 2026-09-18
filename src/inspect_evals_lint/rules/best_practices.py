"""Best-practice checks: late model resolution, stable sample IDs, overridable task parameters."""

from __future__ import annotations

import ast
from collections.abc import Iterable

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.models import LintResult
from inspect_evals_lint.registry import rule
from inspect_evals_lint.rules._ast import (
    Issue,
    get_call_name,
    get_decorator_name,
    parse_error_result,
    parse_python_files,
)


class GetModelVisitor(ast.NodeVisitor):
    """Record every ``get_model()`` call with whether it sits inside a ``@solver``/``@scorer``."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []  # (line, context, is_valid)
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
        self.generic_visit(node)


@rule(
    code="IEBP001",
    name="get_model_location",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="get_model() is only called inside @solver or @scorer functions",
)
def get_model_location(ctx: LintContext) -> Iterable[LintResult]:
    """Warn when ``get_model()`` is called outside a ``@solver`` or ``@scorer``.

    Resolving concrete models late keeps tasks declarative and configurable.
    """
    eval_path = ctx.path
    parse_results = parse_python_files(eval_path)
    if failed := parse_error_result("get_model_location", parse_results.failed_paths):
        yield failed
        return

    issues: list[Issue] = []
    for parsed in parse_results.parsed:
        visitor = GetModelVisitor()
        visitor.visit(parsed.tree)
        issues.extend(
            Issue(str(parsed.path), line, context)
            for line, context, is_valid in visitor.calls
            if not is_valid
        )

    if issues:
        # One result per call site so a line-level `# noautolint: get_model_location` works.
        for issue in issues:
            yield LintResult(
                name="get_model_location",
                status="warn",
                message=(
                    "get_model() called outside @solver/@scorer. "
                    "Resolve models inside @solver/@scorer so tasks stay declarative."
                    + (f" ({issue.detail})" if issue.detail else "")
                ),
                file=issue.file,
                line=issue.line,
            )

    else:
        yield LintResult(
            name="get_model_location",
            status="pass",
            message="get_model() calls are properly inside @solver/@scorer decorated functions",
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

    def visit_Call(self, node: ast.Call) -> None:
        if get_call_name(node) == "Sample":
            has_id = any(keyword.arg == "id" for keyword in node.keywords)
            self.samples.append((node.lineno, has_id))
        self.generic_visit(node)


@rule(
    code="IEBP003",
    name="sample_ids",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="Every Sample() passes id=",
)
def sample_ids(ctx: LintContext) -> Iterable[LintResult]:
    """Fail when a ``Sample()`` call omits ``id=``; stable IDs survive shuffles and reruns."""
    eval_path = ctx.path
    parse_results = parse_python_files(eval_path)
    if failed := parse_error_result("sample_ids", parse_results.failed_paths):
        yield failed
        return

    samples_without_id: list[tuple[str, int]] = []
    total_samples = 0
    for parsed in parse_results.parsed:
        visitor = SampleIdVisitor()
        visitor.visit(parsed.tree)
        for line, has_id in visitor.samples:
            total_samples += 1
            if not has_id:
                samples_without_id.append((parsed.path.name, line))

    if total_samples == 0:
        yield LintResult(name="sample_ids", status="skip", message="No Sample() calls found")
        return

    if samples_without_id:
        yield LintResult(
            name="sample_ids",
            status="fail",
            message=f"Sample() calls without id= parameter: {samples_without_id[:5]}",
        )

    else:
        yield LintResult(
            name="sample_ids",
            status="pass",
            message=f"All {total_samples} Sample() calls include id parameter",
        )


class TaskDefaultsVisitor(ast.NodeVisitor):
    """Record ``@task`` functions with, per parameter, whether it has a default."""

    def __init__(self) -> None:
        self.tasks: list[tuple[str, int, dict[str, bool]]] = []  # (name, line, param_defaults)

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
        self.generic_visit(node)


OVERRIDABLE_PARAMS = {"solver", "scorer", "metric", "metrics", "grader", "model"}


@rule(
    code="IEBP004",
    name="task_overridable_defaults",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="@task parameters naming a solver, scorer, metric, grader or model have defaults",
)
def task_overridable_defaults(ctx: LintContext) -> Iterable[LintResult]:
    """Fail when a ``@task`` parameter naming a solver, scorer, metric, grader or model lacks a default."""
    eval_path = ctx.path
    parse_results = parse_python_files(eval_path)
    if failed := parse_error_result("task_overridable_defaults", parse_results.failed_paths):
        yield failed
        return

    issues: list[tuple[str, list[str]]] = []
    total_tasks = 0
    for parsed in parse_results.parsed:
        visitor = TaskDefaultsVisitor()
        visitor.visit(parsed.tree)
        for task_name, _, param_defaults in visitor.tasks:
            total_tasks += 1
            missing_defaults = [
                actual_param
                for param_name in OVERRIDABLE_PARAMS
                for actual_param, has_default in param_defaults.items()
                if param_name in actual_param.lower() and not has_default
            ]
            if missing_defaults:
                issues.append((task_name, missing_defaults))

    if total_tasks == 0:
        yield LintResult(
            name="task_overridable_defaults",
            status="skip",
            message="No @task decorated functions found",
        )

        return

    if issues:
        yield LintResult(
            name="task_overridable_defaults",
            status="fail",
            message=f"Tasks with overridable params lacking defaults: {issues[:3]}",
        )

    else:
        yield LintResult(
            name="task_overridable_defaults",
            status="pass",
            message="Tasks provide defaults for overridable parameters",
        )


class ModelRoleVisitor(ast.NodeVisitor):
    """Record every ``get_model(role=...)`` call with whether the role resolves deliberately."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []  # (line, role, resolves_deliberately)

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


MODEL_ROLE_CHECK = "model_role_resolution"
MODEL_ROLE_ALLOWLIST_LOCATION = "[tool.inspect-evals-lint.model_role_allowlist] in pyproject.toml"
_MODEL_ROLE_ADVICE = (
    "pass an explicit model, pin a default=, or mark it required=True. An unbound "
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
def model_role_resolution(ctx: LintContext) -> Iterable[LintResult]:
    """Fail on ``get_model(role=...)`` calls with no explicit model, no ``default=`` and no ``required=True``.

    Such a role falls back to the model under evaluation when it isn't bound at
    invocation, and the resulting scores still look plausible. ``allowlist``
    entries ``(eval_name, role)`` warn instead of failing so an existing surface
    can be burned down while new violations are blocked, and a stale entry warns
    so it gets removed. One result per call site so line-level suppression works.
    """
    eval_path, allowlist = ctx.path, ctx.config.model_role_allowlist
    eval_name = eval_path.name
    parse_results = parse_python_files(eval_path)
    if failed := parse_error_result(MODEL_ROLE_CHECK, parse_results.failed_paths):
        yield failed
        return

    total_role_calls = 0
    issues: list[tuple[Issue, str]] = []
    for parsed in parse_results.parsed:
        visitor = ModelRoleVisitor()
        visitor.visit(parsed.tree)
        total_role_calls += len(visitor.calls)
        issues.extend(
            (Issue(str(parsed.path), line, f"role={role!r}"), role)
            for line, role, resolves_deliberately in visitor.calls
            if not resolves_deliberately
        )

    seen_allowlisted: set[tuple[str, str]] = set()
    for issue, role in issues:
        if (eval_name, role) in allowlist:
            seen_allowlisted.add((eval_name, role))
            yield LintResult(
                name=MODEL_ROLE_CHECK,
                status="warn",
                message=(
                    f"Allowlisted get_model(role={role!r}) has no deliberate resolution; "
                    f"{_MODEL_ROLE_ADVICE}, then remove the allowlist entry"
                ),
                file=issue.file,
                line=issue.line,
            )

        else:
            yield LintResult(
                name=MODEL_ROLE_CHECK,
                status="fail",
                message=(
                    f"get_model(role={role!r}) has no model, no default= and no "
                    f"required=True; {_MODEL_ROLE_ADVICE}"
                ),
                file=issue.file,
                line=issue.line,
            )

    stale = {(name, role) for (name, role) in allowlist if name == eval_name} - seen_allowlisted
    for _, role in sorted(stale):
        yield LintResult(
            name=MODEL_ROLE_CHECK,
            status="warn",
            message=(
                f"Allowlist entry for role {role!r} is no longer needed; "
                f"remove it from {MODEL_ROLE_ALLOWLIST_LOCATION}"
            ),
        )

    if issues or stale:
        return
    if total_role_calls == 0:
        yield LintResult(
            name=MODEL_ROLE_CHECK,
            status="skip",
            message="No get_model(role=...) calls found",
        )

    else:
        yield LintResult(
            name=MODEL_ROLE_CHECK,
            status="pass",
            message=f"All {total_role_calls} model role call(s) resolve deliberately",
        )
