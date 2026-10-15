"""The fence_tolerant_json rule."""

from pathlib import Path

import pytest

from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding
from inspect_evals_lint.rules.json_extraction import fence_tolerant_json
from tests.conftest import make_monorepo, make_register_repo, make_template_repo, write

HELPER_SOURCE = """
def strip_json_fence(text: str) -> str:
    return text
"""

JUDGE_STYLE = """
async def score(state, target):
    result = await grader.generate(text, config=config)
    judgment = schema.model_validate_json(result.completion)
    return judgment
"""


def _monorepo(root: Path) -> LintConfig:
    config = make_monorepo(root)
    write(root / "src/inspect_evals/utils/code.py", HELPER_SOURCE)
    return config


def _run(root: Path, config: LintConfig, source: str, name: str = "alpha") -> list[Finding]:
    write(config.package_dir(root, name) / "judge.py", source)
    return list(fence_tolerant_json(LintContext.build(root, name, config)))


def _lines(results: list[Finding]) -> list[int | None]:
    return [r.line for r in results if isinstance(r, Diagnostic)]


class TestWhereTheHelperIsAvailable:
    def test_monorepo_with_the_helper_warns(self, tmp_path):
        results = _run(tmp_path, _monorepo(tmp_path), JUDGE_STYLE)
        assert [r.status for r in results] == ["warn"]
        (diagnostic,) = results
        assert isinstance(diagnostic, Diagnostic)
        assert diagnostic.severity == "warning"
        assert diagnostic.file.name == "judge.py"
        assert diagnostic.line == 4
        assert "score()" in diagnostic.message
        assert "model_validate_json()" in diagnostic.message
        assert diagnostic.hint is not None
        assert "strip_json_fence" in diagnostic.hint

    def test_standalone_repo_without_inspect_evals_skips(self, tmp_path):
        results = _run(tmp_path, make_register_repo(tmp_path), JUDGE_STYLE)
        assert [r.status for r in results] == ["skip"]
        assert "not available" in results[0].message

    def test_template_repo_with_its_own_utils_skips(self, tmp_path):
        results = _run(tmp_path, make_template_repo(tmp_path), JUDGE_STYLE)
        assert [r.status for r in results] == ["skip"]

    def test_monorepo_without_the_helper_skips(self, tmp_path):
        results = _run(tmp_path, make_monorepo(tmp_path), JUDGE_STYLE)
        assert [r.status for r in results] == ["skip"]

    def test_standalone_repo_depending_on_inspect_evals_warns(self, tmp_path):
        config = make_register_repo(tmp_path)
        write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "x"\ndependencies = ["inspect-evals>=0.23"]\n',
        )
        results = _run(tmp_path, config, JUDGE_STYLE)
        assert [r.status for r in results] == ["warn"]


class TestWhatIsFlagged:
    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(JUDGE_STYLE, id="model_validate_json"),
            pytest.param(
                """
async def score(state, target):
    result = await grader_model.generate(prompt)
    try:
        output_json = json.loads(result.completion)
    except json.JSONDecodeError:
        output_json = {}
""",
                id="json_loads",
            ),
            pytest.param(
                """
async def parse(self, response):
    result = await self.model.generate(messages, config=config)
    content = result.completion
    if not content:
        raise ParseError("No response")
    return ResponseType.model_validate_json(content)
""",
                id="through_a_name",
            ),
            pytest.param(
                """
async def parse(model, prompt):
    output = await model.generate(prompt)
    return TypeAdapter(Verdict).validate_json(output.completion)
""",
                id="type_adapter",
            ),
            pytest.param(
                """
async def parse(model, prompt):
    return json.loads((await get_model(role="grader").generate(prompt)).completion)
""",
                id="inline_generate",
            ),
        ],
    )
    def test_strict_parse_of_a_generated_completion_warns(self, tmp_path, source):
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert [r.status for r in results] == ["warn"]

    def test_one_diagnostic_per_call(self, tmp_path):
        source = """
async def score(state, target):
    first = await grader.generate(a)
    second = await grader.generate(b)
    return json.loads(first.completion), json.loads(second.completion)


async def other(state, target):
    result = await grader.generate(a)
    return json.loads(result.completion)
"""
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert _lines(results) == [5, 5, 10]

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(
                """
async def score(state, target):
    result = await grader.generate(text)
    return schema.model_validate_json(strip_json_fence(result.completion))
""",
                id="helper_applied",
            ),
            pytest.param(
                """
async def score(state, target):
    result = await grader.generate(text)
    content = result.completion
    content = strip_json_fence(content)
    return json.loads(content)
""",
                id="name_rebound_through_helper",
            ),
            pytest.param(
                """
async def score(state, target):
    return json.loads(state.output.completion)
""",
                id="model_under_test_output",
            ),
            pytest.param(
                """
def load(path):
    return Metadata.model_validate_json(path.read_text())
""",
                id="file_contents",
            ),
            pytest.param(
                """
async def score(state, target):
    result = await sandbox().exec(["cat", "out.json"])
    return json.loads(result.stdout)
""",
                id="sandbox_output",
            ),
            pytest.param(
                """
async def score(state, target):
    result = await grader.generate(text)
    return json.loads(extract_json(result.completion))
""",
                id="already_extracted",
            ),
        ],
    )
    def test_other_parses_pass(self, tmp_path, source):
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert [r.status for r in results] == ["pass"]

    def test_a_name_bound_in_another_function_is_not_followed(self, tmp_path):
        source = """
async def generate_reply(model, prompt):
    result = await model.generate(prompt)
    return result


def parse(result):
    return json.loads(result.completion)
"""
        results = _run(tmp_path, _monorepo(tmp_path), source)
        assert [r.status for r in results] == ["pass"]


class TestWhatIsNotRead:
    def test_test_files_are_not_read(self, tmp_path):
        config = _monorepo(tmp_path)
        write(config.package_dir(tmp_path, "alpha") / "tests/test_judge.py", JUDGE_STYLE)
        results = list(fence_tolerant_json(LintContext.build(tmp_path, "alpha", config)))
        assert [r.status for r in results] == ["pass"]

    def test_unparseable_file_is_reported(self, tmp_path):
        results = _run(tmp_path, _monorepo(tmp_path), "def broken(:\n")
        assert "fail" in [r.status for r in results]
