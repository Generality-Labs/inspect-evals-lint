"""Tests for individual check helpers, ported from inspect_evals."""

import ast
from dataclasses import replace

from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.rules import dependencies
from inspect_evals_lint.rules.code_quality import unscored_reason
from inspect_evals_lint.rules.dependencies import (
    _extract_package_name,
    _get_imports_from_file,
    _get_stdlib_modules,
    _normalize_name,
    external_dependencies,
)
from inspect_evals_lint.rules.file_structure import (
    _find_task_functions,
    _get_exported_names,
)
from inspect_evals_lint.rules.sandbox import (
    gpu_sandbox_check,
    sandbox_image_pinning,
)
from inspect_evals_lint.rules.tests import _has_eval_call
from tests.conftest import context_for


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
        (result,) = external_dependencies(LintContext.build(root, eval_name, config))
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
        assert "'some_extra_pkg' (package: some-extra-pkg)" in result.message


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

    @staticmethod
    def imports(path):
        eager, lazy, error = _get_imports_from_file(path)
        assert error is None
        return set(eager) | set(lazy)

    def test_simple_import(self, tmp_path):
        """Test simple import statement."""
        py_file = tmp_path / "test.py"
        py_file.write_text("import os")
        assert "os" in self.imports(py_file)

    def test_from_import(self, tmp_path):
        """Test from import statement."""
        py_file = tmp_path / "test.py"
        py_file.write_text("from pathlib import Path")
        assert "pathlib" in self.imports(py_file)

    def test_nested_import(self, tmp_path):
        """Test nested module import - should return top-level."""
        py_file = tmp_path / "test.py"
        py_file.write_text("from os.path import join")
        assert "os" in self.imports(py_file)

    def test_multiple_imports(self, tmp_path):
        """Test multiple imports."""
        py_file = tmp_path / "test.py"
        py_file.write_text("""
import os
import sys
from pathlib import Path
from json import loads, dumps
""")
        imports = self.imports(py_file)
        assert {"os", "sys", "pathlib", "json"} <= imports

    def test_aliased_import(self, tmp_path):
        """Test import with alias."""
        py_file = tmp_path / "test.py"
        py_file.write_text("import numpy as np")
        imports = self.imports(py_file)
        assert "numpy" in imports
        assert "np" not in imports

    def test_eager_and_lazy_imports_are_told_apart(self, tmp_path):
        py_file = tmp_path / "test.py"
        py_file.write_text("""
import eager_a
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import typed_only
if True:
    import eager_b
try:
    import guarded
except ImportError:
    guarded = None

def f():
    import in_function
    def g():
        from nested import thing
    return lambda: __import__("x")

async def h():
    import in_async
""")
        eager, lazy, error = _get_imports_from_file(py_file)
        assert error is None
        assert set(eager) == {"eager_a", "eager_b", "typing"}
        assert set(lazy) == {"typed_only", "guarded", "in_function", "nested", "in_async"}
        assert eager["eager_a"] == (2, 1)

    def test_syntax_error_is_reported(self, tmp_path):
        py_file = tmp_path / "test.py"
        py_file.write_text("import (")
        eager, lazy, error = _get_imports_from_file(py_file)
        assert (eager, lazy) == ({}, {})
        assert error


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
    """Tests for the sandbox_image_pinning rule."""

    def run_check(self, tmp_path, compose_content, eval_name="my_eval", allowlist=frozenset()):
        eval_path = tmp_path / eval_name
        eval_path.mkdir()
        (eval_path / "compose.yaml").write_text(compose_content)
        config = replace(PRESETS["template"], sandbox_image_allowlist=allowlist)
        return list(sandbox_image_pinning(context_for(eval_path, config)))

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
        results = list(sandbox_image_pinning(context_for(eval_path)))
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
        assert [r.status for r in results] == ["warn"]
        assert "no longer" in results[0].message
        assert results[0].file.name == "pyproject.toml"

    def test_nested_compose_files_are_checked(self, tmp_path):
        eval_path = tmp_path / "my_eval"
        (eval_path / "challenges" / "foo").mkdir(parents=True)
        (eval_path / "challenges" / "foo" / "compose.yml").write_text(
            "services:\n  default:\n    image: example/untagged\n"
        )
        results = list(sandbox_image_pinning(context_for(eval_path)))
        assert [r.status for r in results] == ["fail"]

    def test_invalid_yaml_warns(self, tmp_path):
        results = self.run_check(tmp_path, "services: [unclosed\n")
        assert [r.status for r in results] == ["warn"]


class TestCheckUnscoredReason:
    @staticmethod
    def _run(tmp_path, source: str):
        eval_dir = tmp_path / "alpha"
        eval_dir.mkdir()
        (eval_dir / "scorer.py").write_text(source, encoding="utf-8")
        return list(unscored_reason(context_for(eval_dir)))

    def test_skips_when_nothing_is_unscored(self, tmp_path):
        results = self._run(tmp_path, "x = Score(value=1)")
        assert [r.status for r in results] == ["skip"]

    def test_passes_when_every_call_gives_a_reason(self, tmp_path):
        source = (
            'a = Score.unscored(reason="grader_failed")\n'
            "b = Score.unscored(reason=reason, metadata={})\n"
            "c = Score.unscored(**kwargs)\n"
        )
        results = self._run(tmp_path, source)
        assert [r.status for r in results] == ["pass"]
        assert "3 Score.unscored() call(s)" in results[0].message

    def test_fails_once_per_call_without_reason(self, tmp_path):
        source = (
            "a = Score.unscored()\n"
            'b = Score.unscored(answer="x", explanation="y")\n'
            "c = Score.unscored(reason=None)\n"
            'd = Score.unscored(reason="")\n'
        )
        results = self._run(tmp_path, source)
        assert [(r.status, r.line) for r in results] == [
            ("fail", 1),
            ("fail", 2),
            ("fail", 3),
            ("fail", 4),
        ]
        assert "without reason=" in results[0].message
        assert results[0].file.name == "scorer.py"

    def test_legacy_metadata_key_fails_wherever_it_appears(self, tmp_path):
        source = (
            'a = Score.unscored(reason="grader_failed", metadata={"unscored_reason": "grader_failed"})\n'
            'mode = score.metadata["unscored_reason"]\n'
            'other = score.metadata.get("unscored_reason")\n'
        )
        results = self._run(tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("fail", 1), ("fail", 2), ("fail", 3)]
        assert all("superseded by Score.reason" in r.message for r in results)

    def test_docstrings_and_comments_mentioning_the_key_are_ignored(self, tmp_path):
        source = (
            '"""Module docstring: we used to write unscored_reason here."""\n'
            "\n"
            "\n"
            "def f():\n"
            '    """Migrated from metadata["unscored_reason"]."""\n'
            "    # unscored_reason is gone\n"
            '    return Score.unscored(reason="grader_failed")\n'
        )
        results = self._run(tmp_path, source)
        assert [r.status for r in results] == ["pass"]

    def test_local_metric_named_unscored_is_not_the_constructor(self, tmp_path):
        source = (
            "@metric\ndef unscored() -> Metric:\n    ...\n\n\nMETRICS = [accuracy(), unscored()]\n"
        )
        results = self._run(tmp_path, source)
        assert [r.status for r in results] == ["skip"]

    def test_line_level_suppression(self, tmp_path):
        from inspect_evals_lint.registry import get_rule
        from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions

        results = self._run(tmp_path, "a = Score.unscored()  # noautolint: unscored_reason\n")
        for r in results:
            r.rule = get_rule("unscored_reason")
        apply_suppressions(results, load_suppressions(tmp_path / "alpha"))
        assert [r.status for r in results] == ["suppressed"]


class TestGpuSandboxCheck:
    """Tests for the gpu_sandbox_check rule."""

    GPU_TASKS = "tasks:\n  - name: my_eval\n    dataset_samples: 10\n"

    def run_check(self, tmp_path, eval_yaml, eval_name="my_eval"):
        eval_path = tmp_path / eval_name
        eval_path.mkdir()
        if eval_yaml is not None:
            (eval_path / "eval.yaml").write_text(eval_yaml)
        return list(gpu_sandbox_check(context_for(eval_path)))

    def test_missing_eval_yaml_skips(self, tmp_path):
        results = self.run_check(tmp_path, None)
        assert [r.status for r in results] == ["skip"]

    def test_no_gpu_requirement_skips(self, tmp_path):
        results = self.run_check(
            tmp_path, self.GPU_TASKS + "metadata:\n  requires:\n    internet: true\n"
        )
        assert [r.status for r in results] == ["skip"]

    def test_gpu_false_skips(self, tmp_path):
        results = self.run_check(
            tmp_path, self.GPU_TASKS + "metadata:\n  requires:\n    gpu: false\n"
        )
        assert [r.status for r in results] == ["skip"]

    def test_gpu_eval_without_check_task_fails(self, tmp_path):
        results = self.run_check(
            tmp_path,
            self.GPU_TASKS + "metadata:\n  requires:\n    gpu:\n      count: 1\n",
        )
        assert [r.status for r in results] == ["fail"]
        assert "sandbox check task" in results[0].message
        assert "kind: maintenance" in (results[0].hint or "")

    def test_gpu_eval_with_maintenance_check_task_passes(self, tmp_path):
        results = self.run_check(
            tmp_path,
            self.GPU_TASKS
            + "  - name: my_eval_sandbox_check\n    dataset_samples: 3\n    kind: maintenance\n"
            + "metadata:\n  requires:\n    gpu: true\n",
        )
        assert [r.status for r in results] == ["pass"]
        assert "my_eval_sandbox_check" in results[0].message

    def test_check_task_must_be_declared_maintenance(self, tmp_path):
        results = self.run_check(
            tmp_path,
            self.GPU_TASKS
            + "  - name: my_eval_sandbox_check\n    dataset_samples: 3\n"
            + "metadata:\n  requires:\n    gpu: true\n",
        )
        assert [r.status for r in results] == ["fail"]
        assert "my_eval_sandbox_check" in results[0].message
        assert "kind: maintenance" in results[0].message

    def test_invalid_yaml_warns(self, tmp_path):
        results = self.run_check(tmp_path, "tasks: [\n")
        assert [r.status for r in results] == ["warn"]
