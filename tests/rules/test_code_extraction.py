"""The shared_code_extraction rule."""

from pathlib import Path

import pytest

from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding
from inspect_evals_lint.rules.code_extraction import shared_code_extraction
from tests.conftest import make_monorepo, make_register_repo, make_template_repo, write

HELPER_SOURCE = """
import re

_FENCE = re.compile(r"^```[ \\t]*([^\\s`]*)[^\\n]*\\r?\\n(.*?)^```", re.DOTALL | re.MULTILINE)


def extract_code_block(completion: str, language: str = "python") -> str | None:
    match = _FENCE.search(completion)
    return match.group(2) if match else None
"""

APPS_STYLE = """
import re


def find_code(completion: str) -> str:
    pattern = re.compile(r"```python\\n(.*?)```", re.DOTALL)
    matches = pattern.findall(completion)
    return matches[0] if matches else completion
"""


def _monorepo(root: Path) -> LintConfig:
    config = make_monorepo(root)
    write(root / "src/inspect_evals/utils/code.py", HELPER_SOURCE)
    return config


def _run(root: Path, config: LintConfig, source: str, name: str = "alpha") -> list[Finding]:
    write(config.package_dir(root, name) / "extract.py", source)
    return list(shared_code_extraction(LintContext.build(root, name, config)))


def _lines(results: list[Finding]) -> list[int | None]:
    return [r.line for r in results if isinstance(r, Diagnostic)]


class TestWhereTheHelperIsAvailable:
    def test_monorepo_with_the_helper_warns(self, tmp_path):
        results = _run(tmp_path, _monorepo(tmp_path), APPS_STYLE)
        assert [r.status for r in results] == ["warn"]
        (diagnostic,) = results
        assert isinstance(diagnostic, Diagnostic)
        assert diagnostic.severity == "warning"
        assert diagnostic.file.name == "extract.py"
        assert diagnostic.line == 6
        assert "find_code()" in diagnostic.message
        assert "re.compile()" in diagnostic.message
        assert diagnostic.hint is not None
        assert "extract_code_block" in diagnostic.hint

    def test_standalone_repo_without_inspect_evals_skips(self, tmp_path):
        results = _run(tmp_path, make_register_repo(tmp_path), APPS_STYLE)
        assert [r.status for r in results] == ["skip"]
        assert "not available" in results[0].message

    def test_template_repo_with_its_own_utils_skips(self, tmp_path):
        results = _run(tmp_path, make_template_repo(tmp_path), APPS_STYLE)
        assert [r.status for r in results] == ["skip"]

    def test_monorepo_without_the_helper_skips(self, tmp_path):
        results = _run(tmp_path, make_monorepo(tmp_path), APPS_STYLE)
        assert [r.status for r in results] == ["skip"]

    def test_standalone_repo_depending_on_inspect_evals_warns(self, tmp_path):
        config = make_register_repo(tmp_path)
        write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "my-eval"\ndependencies = ["inspect-ai", "inspect_evals>=0.22"]\n',
        )
        assert [r.status for r in _run(tmp_path, config, APPS_STYLE)] == ["warn"]

    def test_inspect_evals_in_an_extra_counts(self, tmp_path):
        config = make_register_repo(tmp_path)
        write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "my-eval"\ndependencies = ["inspect-ai"]\n\n'
            '[project.optional-dependencies]\nscoring = ["Inspect-Evals"]\n',
        )
        assert [r.status for r in _run(tmp_path, config, APPS_STYLE)] == ["warn"]

    def test_inspect_evals_as_a_dev_dependency_does_not_count(self, tmp_path):
        config = make_register_repo(tmp_path)
        write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "my-eval"\ndependencies = ["inspect-ai"]\n\n'
            '[dependency-groups]\ndev = ["inspect_evals"]\n',
        )
        assert [r.status for r in _run(tmp_path, config, APPS_STYLE)] == ["skip"]

    @pytest.mark.parametrize(
        "pyproject",
        [
            'project = "x"\n',
            '[project]\nname = "e"\ndependencies = "inspect-evals"\n',
            '[project]\nname = "e"\noptional-dependencies = ["inspect-evals"]\n',
            'dependency-groups = ["inspect-evals"]\n',
        ],
    )
    def test_a_malformed_pyproject_skips_instead_of_crashing(self, tmp_path, pyproject):
        config = make_register_repo(tmp_path)
        write(tmp_path / "pyproject.toml", pyproject)
        assert [r.status for r in _run(tmp_path, config, APPS_STYLE)] == ["skip"]


class TestWhatIsFlagged:
    def test_every_listed_call_with_a_fence_literal(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def a(t):\n"
            '    return re.search(r"```py\\n(.*?)```", t, re.DOTALL)\n'
            "\n"
            "def b(t):\n"
            '    return re.findall(pattern=r"`{3}(.*?)`{3}", string=t)\n'
            "\n"
            "def c(t):\n"
            '    return t.split("```python")[1]\n'
            "\n"
            "def d(t):\n"
            '    return t.replace("```python", "")\n'
            "\n"
            "def e(t):\n"
            '    return extract_from_tags(t, "```python\\n", "\\n```")\n'
            "\n"
            "def f(t):\n"
            '    return t.startswith("```cpp")\n'
        )
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert _lines(results) == [4, 7, 10, 13, 16, 19]

    def test_f_string_and_concatenation(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def a(t, tag):\n"
            '    return re.compile(rf"```{tag}\\s*\\n(.*?)```", re.DOTALL).findall(t)\n'
            "\n"
            "def b(t):\n"
            '    return re.findall("```" + "python\\n(.*?)```", t)\n'
        )
        assert _lines(_run(tmp_path, _monorepo(tmp_path), source)) == [4, 7]

    def test_name_bound_in_the_function_or_module(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            'MARKER = "```python"\n'
            "\n"
            "def a(t):\n"
            '    pattern = r"```(?:\\w+)?\\n(.*?)```"\n'
            "    return re.findall(pattern, t, re.DOTALL)\n"
            "\n"
            "def b(t):\n"
            "    return t.split(MARKER, 1)[1]\n"
        )
        assert _lines(_run(tmp_path, _monorepo(tmp_path), source)) == [7, 10]

    def test_one_diagnostic_per_function(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def find_code(t):\n"
            '    p1 = re.compile(r"```python\\n(.*?)```", re.DOTALL)\n'
            '    p2 = re.compile(r"```\\n(.*?)```", re.DOTALL)\n'
            "    return (p1.findall(t) + p2.findall(t))[0]\n"
        )
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert _lines(results) == [4]
        assert "more at line(s) 5" in results[0].message

    def test_module_level_patterns_are_one_site_per_file(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            '_PY = re.compile(r"```python\\n(.*?)```", re.DOTALL)\n'
            '_ANY = re.compile(r"```\\n(.*?)```", re.DOTALL)\n'
        )
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert _lines(results) == [3]
        assert "module-level code" in results[0].message

    def test_an_eval_function_named_like_the_helper_is_still_flagged(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def extract_code_block(text):\n"
            '    matches = re.findall(r"```(?:\\w+)?\\n(.*?)```", text, re.DOTALL)\n'
            "    return matches[-1] if matches else None\n"
        )
        assert _lines(_run(tmp_path, _monorepo(tmp_path), source)) == [4]

    def test_a_name_inside_a_concatenation(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            'FENCE = "```"\n'
            "\n"
            "def a(t):\n"
            '    return re.findall(FENCE + "python\\n(.*?)" + FENCE, t)\n'
            "\n"
            "def b(t):\n"
            '    return re.findall(FENCE + "json\\n(.*?)" + FENCE, t)\n'
        )
        assert _lines(_run(tmp_path, _monorepo(tmp_path), source)) == [6]

    def test_code_extraction_beside_json_parsing_in_one_function(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def parse(t):\n"
            '    cleaned = re.sub(r"```json\\s*|\\s*```", "", t)\n'
            '    return re.search(r"```python\\n(.*?)```", cleaned, re.DOTALL)\n'
        )
        assert _lines(_run(tmp_path, _monorepo(tmp_path), source)) == [5]

    def test_a_label_alternation_with_another_language(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def parse(t):\n"
            '    return re.findall(r"```(?:json|python)?\\n(.*?)```", t, re.DOTALL)\n'
        )
        assert _lines(_run(tmp_path, _monorepo(tmp_path), source)) == [4]


class TestWhatIsLeftAlone:
    def test_json_fences(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            '_JSON = re.compile(r"```(?:json)?\\s*(\\{.*?\\})\\s*```", re.DOTALL)\n'
            "\n"
            "def a(t):\n"
            '    return re.sub("```(json)*", "", t)\n'
            "\n"
            "def b(t):\n"
            '    return re.findall(r"```json\\n([\\s\\S]*?)\\n```", t)\n'
            "\n"
            "def c(t):\n"
            '    cleaned = re.sub(r"```(?:json)?\\n?", "", t)\n'
            '    return re.sub(r"\\n?```", "", cleaned)\n'
        )
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert [r.status for r in results] == ["pass"]

    def test_fences_that_are_not_a_pattern_or_separator(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def explain(code, stderr):\n"
            '    explanation = "The following code was executed:\\n\\n```python\\n"\n'
            "    explanation += code\n"
            '    explanation += f"See details below.\\n ```python\\n {stderr} \\n ```\\n"\n'
            "    return explanation\n"
            "\n"
            "def checks(t):\n"
            '    if "```" in t and t.count("```") > 1:\n'
            '        return t.replace("<code>", "```")\n'
            '    return re.sub(r"<code>", "```python\\n", t)\n'
        )
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert [r.status for r in results] == ["pass"]

    def test_string_methods_with_a_bare_or_json_fence(self, tmp_path):
        source = (
            "def read_markdown(text):\n"
            "    inside = False\n"
            "    for line in text.splitlines():\n"
            '        if line.startswith("```"):\n'
            "            inside = not inside\n"
            '    return inside, text.split("```")\n'
            "\n"
            "def strip_json(text):\n"
            '    return text.replace("```json", "")\n'
        )
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert [r.status for r in results] == ["pass"]

    def test_a_parameter_shadows_a_module_constant(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            'PATTERN = r"```python\\n(.*?)```"\n'
            "\n"
            "def a(t, PATTERN):\n"
            "    return re.findall(PATTERN, t)\n"
        )
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert [r.status for r in results] == ["pass"]

    def test_test_files_inside_the_package(self, tmp_path):
        config = _monorepo(tmp_path)
        eval_dir = config.package_dir(tmp_path, "alpha")
        write(eval_dir / "test_extract.py", APPS_STYLE)
        write(eval_dir / "tests" / "helpers.py", APPS_STYLE)
        results = list(shared_code_extraction(LintContext.build(tmp_path, "alpha", config)))
        assert [r.status for r in results] == ["pass"]

    def test_the_helpers_own_module(self, tmp_path):
        config = _monorepo(tmp_path)
        results = list(shared_code_extraction(LintContext.build(tmp_path, "utils", config)))
        assert [r.status for r in results] == ["pass"]

    def test_other_helper_modules_are_checked(self, tmp_path):
        config = _monorepo(tmp_path)
        write(tmp_path / "src/inspect_evals/utils/other.py", APPS_STYLE)
        results = list(shared_code_extraction(LintContext.build(tmp_path, "utils", config)))
        assert [r.status for r in results] == ["warn"]
        assert results[0].file.name == "other.py"


def test_line_suppression(tmp_path):
    from inspect_evals_lint.registry import get_rule
    from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions

    config = _monorepo(tmp_path)
    source = APPS_STYLE.replace(
        "re.DOTALL)\n",
        "re.DOTALL)  # inspect-evals-lint: ignore[IECQ006] -- upstream takes the last block\n",
        1,
    )
    results = _run(tmp_path, config, source)
    rule = get_rule("IECQ006")
    assert rule is not None
    diagnostics = [r for r in results if isinstance(r, Diagnostic)]
    for diagnostic in diagnostics:
        diagnostic.rule = rule
    ctx = LintContext.build(tmp_path, "alpha", config)
    apply_suppressions(diagnostics, load_suppressions(ctx), config, tmp_path)
    assert [d.status for d in diagnostics] == ["suppressed"]
