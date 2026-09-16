"""Tests for individual check helpers, ported from inspect_evals."""

import ast

from inspect_evals_lint.checks import dependencies
from inspect_evals_lint.checks.dependencies import (
    _extract_package_name,
    _get_imports_from_file,
    _get_stdlib_modules,
    _normalize_name,
    check_external_dependencies,
)
from inspect_evals_lint.checks.file_structure import (
    _find_task_functions,
    _get_exported_names,
)
from inspect_evals_lint.checks.sandbox import check_sandbox_image_pinning
from inspect_evals_lint.checks.tests import _has_eval_call
from inspect_evals_lint.models import LintReport


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


class TestExtractPackageName:
    """Test the _extract_package_name function from dependencies.py."""

    def test_simple_package(self):
        """Test simple package name."""
        assert _extract_package_name("requests") == "requests"

    def test_package_with_version(self):
        """Test package with version specifier."""
        assert _extract_package_name("requests>=2.0") == "requests"
        assert _extract_package_name("numpy<2.0") == "numpy"
        assert _extract_package_name("pandas==1.5.0") == "pandas"

    def test_package_with_extras(self):
        """Test package with extras."""
        assert _extract_package_name("requests[security]") == "requests"

    def test_package_with_environment_marker(self):
        """Test package with environment marker."""
        assert _extract_package_name("pywin32; sys_platform == 'win32'") == "pywin32"

    def test_package_with_trailing_space(self):
        """Test package with trailing space (gets stripped)."""
        assert _extract_package_name("requests ") == "requests"

    def test_empty_string(self):
        """Test empty string returns None."""
        assert _extract_package_name("") is None

    def test_complex_specifier(self):
        """Test complex version specifier."""
        assert _extract_package_name("torch>=1.0,<2.0") == "torch"

    def test_not_equal_and_compatible_specifiers(self):
        assert _extract_package_name("requests!=2.0") == "requests"
        assert _extract_package_name("requests~=2.0") == "requests"

    def test_url_requirement(self):
        assert _extract_package_name("mypkg @ https://example.com/mypkg.whl") == "mypkg"

    def test_name_is_normalised(self):
        """Underscores, dots and case collapse to the PEP 503 form so spellings compare equal."""
        assert _extract_package_name("inspect_ai>=0.3") == "inspect-ai"
        assert _extract_package_name("Inspect-AI") == "inspect-ai"
        assert _extract_package_name("zope.interface") == "zope-interface"
        assert _extract_package_name("a__b--c..d") == "a-b-c-d"


class TestNormalizeName:
    def test_pep503_forms(self):
        assert _normalize_name("Friendly_Bard") == "friendly-bard"
        assert _normalize_name("friendly.bard") == "friendly-bard"
        assert _normalize_name("FRIENDLY-BARD") == "friendly-bard"
        assert _normalize_name("friendly_-_bard") == "friendly-bard"

    def test_import_map_values_are_normalised(self, monkeypatch):
        """Distribution metadata may spell a name either way; the map stores the canonical form."""
        monkeypatch.setattr(
            dependencies, "packages_distributions", lambda: {"foo": ["Foo_Bar"], "none": []}
        )
        dependencies._get_import_to_package_map.cache_clear()
        try:
            mapping = dependencies._get_import_to_package_map()
            assert mapping["foo"] == "foo-bar"
            assert "none" not in mapping
            assert mapping["sklearn"] == "scikit-learn"
        finally:
            dependencies._get_import_to_package_map.cache_clear()


class TestCheckExternalDependenciesNormalisation:
    """The declared spelling of a dependency must not matter."""

    @staticmethod
    def _status(root, config, eval_name="alpha"):
        report = LintReport(eval_name=eval_name)
        check_external_dependencies(
            root, eval_name, config.eval_dir(root, eval_name), config, report
        )
        (result,) = report.results
        return result

    def test_hyphenated_core_dependency_covers_underscore_import(self, template_repo):
        """``import inspect_ai`` is satisfied by ``dependencies = ["inspect-ai"]``."""
        root, config = template_repo
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text().replace(
                'dependencies = ["inspect_ai"]', 'dependencies = ["Inspect-AI>=0.3"]'
            )
        )
        assert "inspect_ai" in (config.eval_dir(root, "alpha") / "alpha.py").read_text()
        assert self._status(root, config).status == "pass"

    def test_hyphenated_optional_dependency_covers_underscore_import(self, template_repo):
        root, config = template_repo
        (config.eval_dir(root, "alpha") / "alpha.py").write_text(
            "import some_extra_pkg\n" + (config.eval_dir(root, "alpha") / "alpha.py").read_text()
        )
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text()
            + '\n[project.optional-dependencies]\nalpha = ["Some.Extra-Pkg>=1"]\n'
        )
        assert self._status(root, config).status == "pass"

    def test_undeclared_import_still_fails(self, template_repo):
        root, config = template_repo
        (config.eval_dir(root, "alpha") / "alpha.py").write_text(
            "import some_extra_pkg\n" + (config.eval_dir(root, "alpha") / "alpha.py").read_text()
        )
        result = self._status(root, config)
        assert result.status == "fail"
        assert "some_extra_pkg (package: some-extra-pkg)" in result.message


class TestGetStdlibModules:
    """Test the _get_stdlib_modules function from dependencies.py."""

    def test_returns_frozenset(self):
        """Test that result is a frozenset."""
        result = _get_stdlib_modules()
        assert isinstance(result, frozenset)

    def test_contains_common_modules(self):
        """Test that common stdlib modules are included."""
        result = _get_stdlib_modules()
        assert "os" in result
        assert "sys" in result
        assert "json" in result
        assert "pathlib" in result
        assert "ast" in result
        assert "re" in result

    def test_contains_typing_extensions(self):
        """Test that typing_extensions is included as a common backport."""
        result = _get_stdlib_modules()
        assert "typing_extensions" in result

    def test_does_not_contain_third_party(self):
        """Test that third-party packages are not included."""
        result = _get_stdlib_modules()
        assert "requests" not in result
        assert "numpy" not in result
        assert "pandas" not in result


class TestGetImportsFromFile:
    """Test the _get_imports_from_file function from dependencies.py."""

    def test_simple_import(self, tmp_path):
        """Test simple import statement."""
        py_file = tmp_path / "test.py"
        py_file.write_text("import os")
        imports, error = _get_imports_from_file(py_file)
        assert error is None
        assert "os" in imports

    def test_from_import(self, tmp_path):
        """Test from import statement."""
        py_file = tmp_path / "test.py"
        py_file.write_text("from pathlib import Path")
        imports, error = _get_imports_from_file(py_file)
        assert error is None
        assert "pathlib" in imports

    def test_nested_import(self, tmp_path):
        """Test nested module import - should return top-level."""
        py_file = tmp_path / "test.py"
        py_file.write_text("from os.path import join")
        imports, error = _get_imports_from_file(py_file)
        assert error is None
        assert "os" in imports

    def test_multiple_imports(self, tmp_path):
        """Test multiple imports."""
        py_file = tmp_path / "test.py"
        py_file.write_text("""
import os
import sys
from pathlib import Path
from json import loads, dumps
""")
        imports, error = _get_imports_from_file(py_file)
        assert error is None
        assert "os" in imports
        assert "sys" in imports
        assert "pathlib" in imports
        assert "json" in imports

    def test_aliased_import(self, tmp_path):
        """Test import with alias."""
        py_file = tmp_path / "test.py"
        py_file.write_text("import numpy as np")
        imports, error = _get_imports_from_file(py_file)
        assert error is None
        assert "numpy" in imports
        assert "np" not in imports


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


class TestSandboxImagePinning:
    """Tests for check_sandbox_image_pinning."""

    def run_check(self, tmp_path, compose_content, eval_name="my_eval", allowlist=frozenset()):
        eval_path = tmp_path / eval_name
        eval_path.mkdir()
        (eval_path / "compose.yaml").write_text(compose_content)
        report = LintReport(eval_name=eval_name)
        check_sandbox_image_pinning(eval_path, report, allowlist)
        return [r for r in report.results if r.name == "sandbox_image_pinning"]

    def test_untagged_registry_image_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: aisiuk/inspect-tool-support\n",
        )
        assert [r.status for r in results] == ["fail"]
        assert "aisiuk/inspect-tool-support" in results[0].message

    def test_latest_tag_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: ghcr.io/example/thing:latest\n",
        )
        assert [r.status for r in results] == ["fail"]

    def test_version_tag_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: collabora/code:24.04.9.2.1\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_build_date_tag_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: ghcr.io/generality-labs/inspect-eval-ds10000:2026-08-03\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_tag_and_digest_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: aisiuk/evals-cybench-agent-sandbox:1.0.0@sha256:"
            + "a" * 64
            + "\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_digest_only_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: aisiuk/thing@sha256:" + "b" * 64 + "\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_registry_port_untagged_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: localhost:5000/my-image\n",
        )
        assert [r.status for r in results] == ["fail"]

    def test_built_service_is_ignored(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    build: .\n    image: my-local-image\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_env_interpolated_image_is_ignored(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: ${SAMPLE_METADATA_IMAGE_FIXED}\n",
        )
        assert [r.status for r in results] == ["pass"]

    def test_no_compose_files_skips(self, tmp_path):
        eval_path = tmp_path / "my_eval"
        eval_path.mkdir()
        report = LintReport(eval_name="my_eval")
        check_sandbox_image_pinning(eval_path, report)
        results = [r for r in report.results if r.name == "sandbox_image_pinning"]
        assert [r.status for r in results] == ["skip"]

    def test_allowlisted_image_warns(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: example/untagged\n",
            allowlist=frozenset({("my_eval", "example/untagged")}),
        )
        assert [r.status for r in results] == ["warn"]

    def test_stale_allowlist_entry_warns(self, tmp_path):
        results = self.run_check(
            tmp_path,
            "services:\n  default:\n    image: example/pinned:1.0.0\n",
            allowlist=frozenset({("my_eval", "example/untagged")}),
        )
        statuses = sorted(r.status for r in results)
        assert statuses == ["pass", "warn"]
        warn = next(r for r in results if r.status == "warn")
        assert "no longer" in warn.message

    def test_nested_compose_files_are_checked(self, tmp_path):
        eval_path = tmp_path / "my_eval"
        (eval_path / "challenges" / "foo").mkdir(parents=True)
        (eval_path / "challenges" / "foo" / "compose.yml").write_text(
            "services:\n  default:\n    image: example/untagged\n"
        )
        report = LintReport(eval_name="my_eval")
        check_sandbox_image_pinning(eval_path, report)
        results = [r for r in report.results if r.name == "sandbox_image_pinning"]
        assert [r.status for r in results] == ["fail"]

    def test_invalid_yaml_warns(self, tmp_path):
        results = self.run_check(tmp_path, "services: [unclosed\n")
        assert [r.status for r in results] == ["warn"]
