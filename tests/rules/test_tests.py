"""Rules about the test tree."""

import ast

from inspect_evals_lint.rules.tests import _has_eval_call


class TestHasEvalCall:
    """Test the _has_eval_call function from tests.py."""

    def test_direct_eval_call(self):
        """Test detection of direct eval() call."""
        code = "eval(tasks=task, model=model)"
        tree = ast.parse(code)
        assert _has_eval_call(tree) is True

    def test_module_eval_call(self):
        """Test detection of inspect_ai.eval() call."""
        code = "inspect_ai.eval(tasks=task)"
        tree = ast.parse(code)
        assert _has_eval_call(tree) is True

    def test_no_eval_call(self):
        """Test code without eval() call."""
        code = "result = run_tests()"
        tree = ast.parse(code)
        assert _has_eval_call(tree) is False

    def test_eval_in_function(self):
        """Test eval() call inside a function."""
        code = """
def test_e2e():
    result = eval(tasks=my_task(), model="mockllm/model")
    assert result
"""
        tree = ast.parse(code)
        assert _has_eval_call(tree) is True

    def test_different_eval_not_detected(self):
        """Test that evaluate() is not detected as eval()."""
        code = "evaluate(data)"
        tree = ast.parse(code)
        assert _has_eval_call(tree) is False
