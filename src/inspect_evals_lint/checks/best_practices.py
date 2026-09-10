"""Best-practice checks: late model resolution, stable sample IDs, overridable task parameters."""

from __future__ import annotations

import ast
from pathlib import Path

from inspect_evals_lint.checks.utils import (
    Issue,
    add_parse_errors_to_report,
    get_call_name,
    get_decorator_name,
    parse_python_files,
)
from inspect_evals_lint.models import LintReport, LintResult


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


def check_get_model_location(eval_path: Path, report: LintReport) -> None:
    """Warn when ``get_model()`` is called outside a ``@solver`` or ``@scorer``.

    Resolving concrete models late keeps tasks declarative and configurable.
    """
    parse_results = parse_python_files(eval_path)
    if add_parse_errors_to_report("get_model_location", parse_results.failed_paths, report):
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
            report.add(
                LintResult(
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
            )
    else:
        report.add(
            LintResult(
                name="get_model_location",
                status="pass",
                message="get_model() calls are properly inside @solver/@scorer decorated functions",
            )
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


def check_sample_ids(eval_path: Path, report: LintReport) -> None:
    """Fail when a ``Sample()`` call omits ``id=``; stable IDs survive shuffles and reruns."""
    parse_results = parse_python_files(eval_path)
    if add_parse_errors_to_report("sample_ids", parse_results.failed_paths, report):
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
        report.add(LintResult(name="sample_ids", status="skip", message="No Sample() calls found"))
        return

    if samples_without_id:
        report.add(
            LintResult(
                name="sample_ids",
                status="fail",
                message=f"Sample() calls without id= parameter: {samples_without_id[:5]}",
            )
        )
    else:
        report.add(
            LintResult(
                name="sample_ids",
                status="pass",
                message=f"All {total_samples} Sample() calls include id parameter",
            )
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


def check_task_overridable_defaults(eval_path: Path, report: LintReport) -> None:
    """Fail when a ``@task`` parameter naming a solver, scorer, metric, grader or model lacks a default."""
    parse_results = parse_python_files(eval_path)
    if add_parse_errors_to_report("task_overridable_defaults", parse_results.failed_paths, report):
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
        report.add(
            LintResult(
                name="task_overridable_defaults",
                status="skip",
                message="No @task decorated functions found",
            )
        )
        return

    if issues:
        report.add(
            LintResult(
                name="task_overridable_defaults",
                status="fail",
                message=f"Tasks with overridable params lacking defaults: {issues[:3]}",
            )
        )
    else:
        report.add(
            LintResult(
                name="task_overridable_defaults",
                status="pass",
                message="Tasks provide defaults for overridable parameters",
            )
        )
