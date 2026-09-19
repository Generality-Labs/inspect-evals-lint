"""File-structure rule helpers."""

from inspect_evals_lint.rules.file_structure import (
    _find_task_functions,
    _get_exported_names,
)


class TestFindTaskFunctions:
    """Test the _find_task_functions function from file_structure.py."""

    def test_finds_task_function(self, tmp_path):
        """Test finding @task decorated function."""
        py_file = tmp_path / "eval.py"
        py_file.write_text("""
from inspect_ai import task

@task
def my_eval():
    pass
""")
        result = _find_task_functions(py_file)
        assert result == ["my_eval"]

    def test_finds_multiple_task_functions(self, tmp_path):
        """Test finding multiple @task decorated functions."""
        py_file = tmp_path / "eval.py"
        py_file.write_text("""
from inspect_ai import task

@task
def eval_one():
    pass

@task
def eval_two():
    pass

def helper():
    pass
""")
        result = _find_task_functions(py_file)
        assert set(result) == {"eval_one", "eval_two"}

    def test_ignores_other_decorators(self, tmp_path):
        """Test that non-@task decorators are not matched."""
        py_file = tmp_path / "eval.py"
        py_file.write_text("""
from inspect_ai.solver import solver
from inspect_ai.scorer import scorer

@solver
def my_solver():
    pass

@scorer
def my_scorer():
    pass
""")
        result = _find_task_functions(py_file)
        assert result == []

    def test_nonexistent_file_returns_empty(self, tmp_path):
        """Test that nonexistent file returns empty list."""
        py_file = tmp_path / "nonexistent.py"
        result = _find_task_functions(py_file)
        assert result == []

    def test_task_with_call_decorator(self, tmp_path):
        """Test @task() with parentheses."""
        py_file = tmp_path / "eval.py"
        py_file.write_text("""
from inspect_ai import task

@task()
def my_eval():
    pass
""")
        result = _find_task_functions(py_file)
        assert result == ["my_eval"]


class TestGetExportedNames:
    """Test the _get_exported_names function from file_structure.py."""

    def test_finds_all_exports(self, tmp_path):
        """Test finding names in __all__."""
        init_file = tmp_path / "__init__.py"
        init_file.write_text('__all__ = ["my_eval", "helper"]')
        result = _get_exported_names(init_file)
        assert result == {"my_eval", "helper"}

    def test_finds_import_exports(self, tmp_path):
        """Test finding names from import statements."""
        init_file = tmp_path / "__init__.py"
        init_file.write_text("""
from .my_eval import my_eval
from .helper import helper_func as helper
""")
        result = _get_exported_names(init_file)
        assert result == {"my_eval", "helper"}

    def test_combines_all_and_imports(self, tmp_path):
        """Test that __all__ and imports are both captured."""
        init_file = tmp_path / "__init__.py"
        init_file.write_text("""
from .my_eval import my_eval

__all__ = ["my_eval", "CONSTANT"]
""")
        result = _get_exported_names(init_file)
        assert "my_eval" in result
        assert "CONSTANT" in result

    def test_nonexistent_file_returns_empty(self, tmp_path):
        """Test that nonexistent file returns empty set."""
        init_file = tmp_path / "__init__.py"
        result = _get_exported_names(init_file)
        assert result == set()

    def test_tuple_all(self, tmp_path):
        """Test __all__ as tuple."""
        init_file = tmp_path / "__init__.py"
        init_file.write_text('__all__ = ("my_eval", "helper")')
        result = _get_exported_names(init_file)
        assert result == {"my_eval", "helper"}
