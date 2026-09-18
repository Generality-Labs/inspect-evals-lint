"""Best-practice rules and their AST visitors."""

import ast
from pathlib import Path

import pytest

from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.rules.best_practices import (
    GetModelVisitor,
    ModelRoleVisitor,
    SampleIdVisitor,
    TaskDefaultsVisitor,
    TaskParameterVisitor,
    model_role_resolution,
)
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
from tests.conftest import context_for


class TestGetModelVisitor:
    """Test the GetModelVisitor class."""

    def test_get_model_in_solver(self):
        """Test that get_model() inside @solver is marked valid."""
        code = """
@solver
def my_solver():
    model = get_model()
    return model
"""
        tree = ast.parse(code)
        visitor = GetModelVisitor()
        visitor.visit(tree)

        assert len(visitor.calls) == 1
        _line, context, is_valid = visitor.calls[0]
        assert context == "my_solver"
        assert is_valid is True

    def test_get_model_in_scorer(self):
        """Test that get_model() inside @scorer is marked valid."""
        code = """
@scorer
def my_scorer():
    model = get_model()
    return model
"""
        tree = ast.parse(code)
        visitor = GetModelVisitor()
        visitor.visit(tree)

        assert len(visitor.calls) == 1
        _line, context, is_valid = visitor.calls[0]
        assert context == "my_scorer"
        assert is_valid is True

    def test_get_model_outside_solver_scorer(self):
        """Test that get_model() outside @solver/@scorer is marked invalid."""
        code = """
def my_function():
    model = get_model()
    return model
"""
        tree = ast.parse(code)
        visitor = GetModelVisitor()
        visitor.visit(tree)

        assert len(visitor.calls) == 1
        _line, context, is_valid = visitor.calls[0]
        assert context == "my_function"
        assert is_valid is False

    def test_get_model_at_module_level(self):
        """Test that get_model() at module level is marked invalid."""
        code = "model = get_model()"
        tree = ast.parse(code)
        visitor = GetModelVisitor()
        visitor.visit(tree)

        assert len(visitor.calls) == 1
        _line, context, is_valid = visitor.calls[0]
        assert context == "module"
        assert is_valid is False

    def test_no_get_model_calls(self):
        """Test code without get_model() calls."""
        code = """
@solver
def my_solver():
    return "hello"
"""
        tree = ast.parse(code)
        visitor = GetModelVisitor()
        visitor.visit(tree)

        assert len(visitor.calls) == 0

    def test_get_model_with_attribute_call(self):
        """Test module.get_model() pattern."""
        code = """
@solver
def my_solver():
    model = inspect_ai.get_model()
    return model
"""
        tree = ast.parse(code)
        visitor = GetModelVisitor()
        visitor.visit(tree)

        assert len(visitor.calls) == 1
        assert visitor.calls[0][2] is True  # is_valid


class TestSampleIdVisitor:
    """Test the SampleIdVisitor class."""

    def test_sample_with_id(self):
        """Test Sample() with id parameter."""
        code = 'Sample(input="test", id="sample_1")'
        tree = ast.parse(code)
        visitor = SampleIdVisitor()
        visitor.visit(tree)

        assert len(visitor.samples) == 1
        _line, has_id = visitor.samples[0]
        assert has_id is True

    def test_sample_without_id(self):
        """Test Sample() without id parameter."""
        code = 'Sample(input="test", target="answer")'
        tree = ast.parse(code)
        visitor = SampleIdVisitor()
        visitor.visit(tree)

        assert len(visitor.samples) == 1
        _line, has_id = visitor.samples[0]
        assert has_id is False

    def test_multiple_samples(self):
        """Test multiple Sample() calls."""
        code = """
Sample(input="a", id="1")
Sample(input="b")
Sample(input="c", id="3")
"""
        tree = ast.parse(code)
        visitor = SampleIdVisitor()
        visitor.visit(tree)

        assert len(visitor.samples) == 3
        assert visitor.samples[0][1] is True  # has id
        assert visitor.samples[1][1] is False  # no id
        assert visitor.samples[2][1] is True  # has id

    def test_no_sample_calls(self):
        """Test code without Sample() calls."""
        code = "foo()"
        tree = ast.parse(code)
        visitor = SampleIdVisitor()
        visitor.visit(tree)

        assert len(visitor.samples) == 0

    def test_samples_in_list_comprehension(self):
        """Test Sample() calls generated via list comprehension."""
        code = '[Sample(input=x, id=f"id_{i}") for i, x in enumerate(data)]'
        tree = ast.parse(code)
        visitor = SampleIdVisitor()
        visitor.visit(tree)

        # The visitor should find the Sample call inside the comprehension
        assert len(visitor.samples) == 1
        _line, has_id = visitor.samples[0]
        assert has_id is True

    def test_samples_in_list_comprehension_without_id(self):
        """Test Sample() calls in list comprehension without id."""
        code = "[Sample(input=x, target=y) for x, y in pairs]"
        tree = ast.parse(code)
        visitor = SampleIdVisitor()
        visitor.visit(tree)

        assert len(visitor.samples) == 1
        _line, has_id = visitor.samples[0]
        assert has_id is False


class TestTaskParameterVisitor:
    """Test the TaskParameterVisitor class."""

    def test_task_with_dataset_param(self):
        """Test @task function with dataset parameter."""
        code = """
@task
def my_task(dataset=None):
    pass
"""
        tree = ast.parse(code)
        visitor = TaskParameterVisitor()
        visitor.visit(tree)

        assert len(visitor.tasks) == 1
        name, _line, params = visitor.tasks[0]
        assert name == "my_task"
        assert "dataset" in params

    def test_task_without_dataset_param(self):
        """Test @task function without dataset parameter."""
        code = """
@task
def my_task(limit=10):
    pass
"""
        tree = ast.parse(code)
        visitor = TaskParameterVisitor()
        visitor.visit(tree)

        assert len(visitor.tasks) == 1
        name, _line, params = visitor.tasks[0]
        assert name == "my_task"
        assert "dataset" not in params
        assert "limit" in params

    def test_non_task_function(self):
        """Test that non-@task functions are not recorded."""
        code = """
def my_function(dataset=None):
    pass
"""
        tree = ast.parse(code)
        visitor = TaskParameterVisitor()
        visitor.visit(tree)

        assert len(visitor.tasks) == 0

    def test_task_with_kwonly_params(self):
        """Test @task function with keyword-only parameters."""
        code = """
@task
def my_task(*, dataset=None, solver=None):
    pass
"""
        tree = ast.parse(code)
        visitor = TaskParameterVisitor()
        visitor.visit(tree)

        assert len(visitor.tasks) == 1
        _name, _line, params = visitor.tasks[0]
        assert "dataset" in params
        assert "solver" in params


class TestTaskDefaultsVisitor:
    """Test the TaskDefaultsVisitor class."""

    def test_task_with_defaults(self):
        """Test @task function with default values."""
        code = """
@task
def my_task(solver=None, scorer=None):
    pass
"""
        tree = ast.parse(code)
        visitor = TaskDefaultsVisitor()
        visitor.visit(tree)

        assert len(visitor.tasks) == 1
        _name, _line, param_defaults = visitor.tasks[0]
        assert param_defaults["solver"] is True
        assert param_defaults["scorer"] is True

    def test_task_without_defaults(self):
        """Test @task function without default values."""
        code = """
@task
def my_task(solver, scorer):
    pass
"""
        tree = ast.parse(code)
        visitor = TaskDefaultsVisitor()
        visitor.visit(tree)

        assert len(visitor.tasks) == 1
        _name, _line, param_defaults = visitor.tasks[0]
        assert param_defaults["solver"] is False
        assert param_defaults["scorer"] is False

    def test_task_mixed_defaults(self):
        """Test @task function with some defaults."""
        code = """
@task
def my_task(solver, scorer=None):
    pass
"""
        tree = ast.parse(code)
        visitor = TaskDefaultsVisitor()
        visitor.visit(tree)

        assert len(visitor.tasks) == 1
        _name, _line, param_defaults = visitor.tasks[0]
        assert param_defaults["solver"] is False
        assert param_defaults["scorer"] is True


class TestModelRoleVisitor:
    """Ported from inspect_evals (UKGovernmentBEIS/inspect_evals#2321)."""

    def _visit(self, code: str):
        visitor = ModelRoleVisitor()
        visitor.visit(ast.parse(code))
        return visitor.calls

    def test_bare_role_is_flagged(self):
        (call,) = self._visit('grader = get_model(role="grader")')
        assert call == (1, "grader", False)

    @pytest.mark.parametrize(
        "code",
        [
            'get_model(role="grader", required=True)',
            'get_model(role="grader", default="openai/gpt-4o")',
            'get_model(model=judge_llm, role="grader")',
            'get_model(judge_llm, role="grader")',
            'get_model(role="grader", required=strict)',  # not knowable statically
        ],
    )
    def test_deliberate_resolutions_are_accepted(self, code: str):
        assert self._visit(code)[0][2] is True

    @pytest.mark.parametrize(
        "code",
        [
            'get_model(model=None, role="grader")',
            'get_model(role="grader", default=None)',
            'get_model(role="grader", required=False)',
        ],
    )
    def test_no_op_arguments_do_not_silence_the_check(self, code: str):
        assert self._visit(code)[0][2] is False

    def test_attribute_call_is_matched(self):
        (call,) = self._visit('grader = inspect_model.get_model(role="grader")')
        assert call[1] == "grader"

    def test_call_without_role_is_ignored(self):
        assert self._visit("model = get_model()") == []
        assert self._visit('model = get_model("openai/gpt-4o")') == []

    def test_dynamic_role_name_is_a_placeholder(self):
        assert self._visit("get_model(role=role_name, required=True)")[0][1] == "<dynamic>"

    def test_multiline_call_reports_the_call_start_line(self):
        assert self._visit('x = get_model(\n    role="grader",\n)')[0][0] == 1


class TestCheckModelRoleResolution:
    @staticmethod
    def _run(eval_dir: Path, source: str):
        eval_dir.mkdir(exist_ok=True)
        (eval_dir / "scorer.py").write_text(source, encoding="utf-8")
        return list(model_role_resolution(context_for(eval_dir)))

    def test_skips_when_no_role_calls(self, tmp_path: Path):
        results = self._run(tmp_path / "alpha", "x = get_model()")
        assert [r.status for r in results] == ["skip"]

    def test_passes_when_all_roles_resolve(self, tmp_path: Path):
        results = self._run(tmp_path / "alpha", 'x = get_model(role="grader", required=True)')
        assert [r.status for r in results] == ["pass"]
        assert "1 model role call" in results[0].message

    def test_fails_once_per_offending_call_site(self, tmp_path: Path):
        source = 'a = get_model(role="grader")\nb = get_model(role="judge", default=None)\n'
        results = self._run(tmp_path / "alpha", source)
        assert [r.status for r in results] == ["fail", "fail"]
        assert [r.line for r in results] == [1, 2]
        assert results[0].file.name == "scorer.py"
        assert results[0].column == 5
        assert "role='grader'" in results[0].message
        assert "role='judge'" in results[1].message

    def test_diagnostics_are_keyed_by_role_for_the_allowlist(self, tmp_path: Path):
        results = self._run(
            tmp_path / "alpha", 'a = get_model(role="grader")\nb = get_model(role=name)\n'
        )
        assert [r.key for r in results] == ["grader", "<dynamic>"]

    def test_line_level_suppression_silences_a_call_site(self, tmp_path: Path):
        eval_dir = tmp_path / "alpha"
        results = self._run(
            eval_dir,
            'x = get_model(role="grader")  # inspect-evals-lint: ignore[model_role_resolution]\n',
        )
        for r in results:
            r.rule = get_rule("model_role_resolution")
        apply_suppressions(
            results, load_suppressions(context_for(eval_dir)), PRESETS["template"], tmp_path
        )
        assert [r.status for r in results] == ["suppressed"]
