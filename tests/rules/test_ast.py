"""Shared AST helpers."""

import ast

from inspect_evals_lint.rules._ast import get_call_name, get_decorator_name


class TestGetDecoratorName:
    """Test the get_decorator_name function."""

    def test_simple_decorator(self):
        """Test @decorator pattern."""
        code = "@solver\ndef foo(): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert get_decorator_name(func.decorator_list[0]) == "solver"

    def test_decorator_with_call(self):
        """Test @decorator() pattern."""
        code = "@solver()\ndef foo(): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert get_decorator_name(func.decorator_list[0]) == "solver"

    def test_module_decorator(self):
        """Test @module.decorator pattern."""
        code = "@inspect_ai.solver\ndef foo(): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert get_decorator_name(func.decorator_list[0]) == "solver"

    def test_module_decorator_with_call(self):
        """Test @module.decorator() pattern."""
        code = "@inspect_ai.solver()\ndef foo(): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert get_decorator_name(func.decorator_list[0]) == "solver"

    def test_subscript_decorator_returns_none(self):
        """Test that subscript decorators return None."""
        code = "@decorators[0]\ndef foo(): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert get_decorator_name(func.decorator_list[0]) is None

    def test_chained_call_decorator_returns_none(self):
        """Test that chained call decorators return None."""
        code = "@get_decorator()()\ndef foo(): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert get_decorator_name(func.decorator_list[0]) is None


class TestGetCallName:
    """Test the get_call_name function."""

    def test_simple_call(self):
        """Test foo() pattern."""
        code = "foo()"
        tree = ast.parse(code)
        call = tree.body[0].value
        assert get_call_name(call) == "foo"

    def test_method_call(self):
        """Test obj.foo() pattern."""
        code = "obj.foo()"
        tree = ast.parse(code)
        call = tree.body[0].value
        assert get_call_name(call) == "foo"

    def test_chained_method_call(self):
        """Test obj.bar.foo() pattern - returns innermost name."""
        code = "obj.bar.foo()"
        tree = ast.parse(code)
        call = tree.body[0].value
        assert get_call_name(call) == "foo"

    def test_subscript_call_returns_none(self):
        """Test callbacks[0]() pattern returns None."""
        code = "callbacks[0]()"
        tree = ast.parse(code)
        call = tree.body[0].value
        assert get_call_name(call) is None

    def test_chained_call_returns_none(self):
        """Test get_func()() pattern returns None."""
        code = "get_func()()"
        tree = ast.parse(code)
        call = tree.body[0].value
        assert get_call_name(call) is None

    def test_lambda_call_returns_none(self):
        """Test (lambda x: x)() pattern returns None."""
        code = "(lambda x: x)(5)"
        tree = ast.parse(code)
        call = tree.body[0].value
        assert get_call_name(call) is None
