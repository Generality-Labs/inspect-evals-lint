"""The external_dependencies rule and its import and name helpers."""

from typing import ClassVar

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.rules import dependencies
from inspect_evals_lint.rules.dependencies import (
    _extract_package_name,
    _get_imports_from_file,
    _get_stdlib_modules,
    _normalize_name,
    external_dependencies,
)


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
    def _status(root, config, name="alpha"):
        (result,) = external_dependencies(LintContext.build(root, name, config))
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
        assert "inspect_ai" in (config.package_dir(root, "alpha") / "alpha.py").read_text()
        assert self._status(root, config).status == "pass"

    def test_hyphenated_optional_dependency_covers_underscore_import(self, template_repo):
        root, config = template_repo
        (config.package_dir(root, "alpha") / "alpha.py").write_text(
            "import some_extra_pkg\n" + (config.package_dir(root, "alpha") / "alpha.py").read_text()
        )
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text()
            + '\n[project.optional-dependencies]\nalpha = ["Some.Extra-Pkg>=1"]\n'
        )
        assert self._status(root, config).status == "pass"

    def test_undeclared_import_still_fails(self, template_repo):
        root, config = template_repo
        (config.package_dir(root, "alpha") / "alpha.py").write_text(
            "import some_extra_pkg\n" + (config.package_dir(root, "alpha") / "alpha.py").read_text()
        )
        result = self._status(root, config)
        assert result.status == "fail"
        assert "'some_extra_pkg' (package: some-extra-pkg)" in result.message


class TestTransitiveCoreDependencies:
    """What a core dependency requires is installed by construction, so importing it needs no declaration."""

    REQUIREMENTS: ClassVar[dict[str, list[str]]] = {
        "inspect-ai": ["pydantic>=2.11", 'anthropic>=0.5; extra == "dev"', "anyio"],
        "pydantic": ["typing-extensions>=4.6", "annotated-types"],
    }

    @staticmethod
    def _status(root, config, name="alpha"):
        (result,) = external_dependencies(LintContext.build(root, name, config))
        return result

    def _fake_requires(self, monkeypatch):
        def fake_requires(dist: str) -> list[str] | None:
            if dist not in self.REQUIREMENTS:
                raise dependencies.PackageNotFoundError(dist)
            return self.REQUIREMENTS[dist]

        monkeypatch.setattr(dependencies, "requires", fake_requires)

    def test_transitive_requirement_is_not_external(self, template_repo, monkeypatch):
        self._fake_requires(monkeypatch)
        root, config = template_repo
        (config.package_dir(root, "alpha") / "alpha.py").write_text(
            "import pydantic\nimport annotated_types\n"
            + (config.package_dir(root, "alpha") / "alpha.py").read_text()
        )
        assert self._status(root, config).status == "pass"

    def test_extras_only_requirement_is_still_external(self, template_repo, monkeypatch):
        """``anthropic`` comes with ``inspect_ai[dev]``, which nobody asked for."""
        self._fake_requires(monkeypatch)
        root, config = template_repo
        (config.package_dir(root, "alpha") / "alpha.py").write_text(
            "import anthropic\n" + (config.package_dir(root, "alpha") / "alpha.py").read_text()
        )
        result = self._status(root, config)
        assert result.status == "fail"
        assert "'anthropic'" in result.message

    def test_uninstalled_core_dependency_contributes_nothing(self, template_repo, monkeypatch):
        monkeypatch.setattr(
            dependencies,
            "requires",
            lambda dist: (_ for _ in ()).throw(dependencies.PackageNotFoundError(dist)),
        )
        root, config = template_repo
        (config.package_dir(root, "alpha") / "alpha.py").write_text(
            "import pydantic\n" + (config.package_dir(root, "alpha") / "alpha.py").read_text()
        )
        assert self._status(root, config).status == "fail"

    def test_closure_terminates_on_cycles(self, monkeypatch):
        monkeypatch.setattr(
            dependencies, "requires", lambda dist: ["a", "b"] if dist == "a" else ["a"]
        )
        assert dependencies._installed_requirements("a") == frozenset({"a", "b"})


class TestPerEvalDependencyGroup:
    """The group-named-after-the-evaluation convention is the monorepo's, not a standalone repository's."""

    @staticmethod
    def _findings(root, config, name="alpha"):
        return list(external_dependencies(LintContext.build(root, name, config)))

    def _repo_with_extra(self, template_repo):
        root, config = template_repo
        (config.package_dir(root, "alpha") / "alpha.py").write_text(
            "import some_extra_pkg\n" + (config.package_dir(root, "alpha") / "alpha.py").read_text()
        )
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text()
            + '\n[project.optional-dependencies]\nmodal = ["some_extra_pkg"]\n'
        )
        return root, config

    def test_any_extra_suffices_by_default(self, template_repo):
        root, config = self._repo_with_extra(template_repo)
        assert config.per_eval_dependency_group is False
        (result,) = self._findings(root, config)
        assert result.status == "pass"

    def test_monorepo_convention_requires_the_named_group(self, template_repo):
        from dataclasses import replace

        root, config = self._repo_with_extra(template_repo)
        (result,) = self._findings(root, replace(config, per_eval_dependency_group=True))
        assert result.status == "fail"
        assert "no dedicated optional-dependency group" in result.message

    def test_hint_names_the_right_home(self, template_repo):
        from dataclasses import replace

        root, config = template_repo
        (config.package_dir(root, "alpha") / "alpha.py").write_text(
            "import some_extra_pkg\n" + (config.package_dir(root, "alpha") / "alpha.py").read_text()
        )
        (standalone,) = self._findings(root, config)
        assert standalone.hint is not None
        assert "[project].dependencies" in standalone.hint
        (monorepo,) = self._findings(root, replace(config, per_eval_dependency_group=True))
        assert monorepo.hint is not None
        assert "group named 'alpha'" in monorepo.hint


class TestSiblingPackagesAreFirstParty:
    """An evaluation importing the repository's own helper or another evaluation declares nothing."""

    def test_helper_and_sibling_eval_imports_are_not_external(self, tmp_path):
        from inspect_evals_lint.config import PRESETS
        from tests.conftest import make_helper, make_template_repo, write

        config = make_template_repo(tmp_path, eval_names=("alpha", "beta"))
        make_helper(tmp_path, config, "utils", "def stable_id(x): return x\n")
        write(
            config.package_dir(tmp_path, "alpha") / "extra.py",
            "from utils.helpers import stable_id\nimport beta\nfrom examples import thing\n",
        )
        write(tmp_path / "src/examples/__init__.py", "")
        results = list(external_dependencies(LintContext.build(tmp_path, "alpha", config)))
        assert [r.status for r in results] == ["pass"], [r.message for r in results]
        assert PRESETS["template"].import_prefix == ""  # nothing else makes these first-party

    def test_a_real_third_party_import_still_fails(self, tmp_path):
        from tests.conftest import make_template_repo, write

        config = make_template_repo(tmp_path)
        write(config.package_dir(tmp_path, "alpha") / "extra.py", "import utils_toolkit\n")
        results = list(external_dependencies(LintContext.build(tmp_path, "alpha", config)))
        assert [r.status for r in results] == ["fail"]


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
        assert eager["eager_a"] == (2, 1, None)

    def test_syntax_error_is_reported(self, tmp_path):
        py_file = tmp_path / "test.py"
        py_file.write_text("import (")
        eager, lazy, error = _get_imports_from_file(py_file)
        assert (eager, lazy) == ({}, {})
        assert error
