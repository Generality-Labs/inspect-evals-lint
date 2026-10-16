"""Scoring rules: grader and sandbox failures inside the code a scorer runs."""

from pathlib import Path

from inspect_evals_lint.config import PRESETS, LintConfig
from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.rules.scoring import scorer_failure_scored
from tests.conftest import context_for, write

HEADER = """
from inspect_ai.model import get_model
from inspect_ai.scorer import CORRECT, INCORRECT, Score, scorer
from inspect_ai.tool import tool
from inspect_ai.util import sandbox
"""


def run(
    tmp_path: Path,
    files: dict[str, str],
    *,
    config: LintConfig | None = None,
    name: str = "my_eval",
):
    package = tmp_path / name
    write(package / "__init__.py", "")
    for relative, code in files.items():
        write(package / relative, HEADER + code)
    return list(scorer_failure_scored(context_for(package, config)))


def diagnostics(results) -> list[Diagnostic]:
    return [r for r in results if isinstance(r, Diagnostic)]


def found(results) -> list[tuple[str, str, int | None]]:
    return [(d.status, d.file.name, d.line) for d in diagnostics(results)]


class TestGraderFailures:
    def test_score_returned_from_a_judge_failure_fails(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def judged():
    async def score(state, target):
        try:
            result = await get_model(role="grader").generate("prompt")
            return Score(value=CORRECT if "yes" in result.completion else INCORRECT)
        except Exception:
            return Score(value=INCORRECT)
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("fail", 13)
        assert "a grader call in score() returns Score(value=INCORRECT)" in d.message
        assert 'Score.unscored(reason="grader_failed")' in (d.hint or "")

    def test_helper_called_from_a_scorer_is_followed(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def _alignment(judge, prompt) -> float:
    try:
        response = await judge.generate(prompt)
        return float(response.completion)
    except Exception as e:
        log(e)
        return 0.0

@scorer(metrics=[])
def aligned():
    async def score(state, target):
        return Score(value=await _alignment(get_model(role="grader"), "p"))
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("fail", 11)
        assert "_alignment() returns 0.0" in d.message

    def test_awaited_judge_or_grader_named_call_counts(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def detection():
    async def score(state, target):
        grader = model_graded_qa()
        try:
            result = await grader(state, target)
            return 1.0, "detected"
        except Exception:
            return 0.5, "uncertain"
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert "returns (0.5, ...)" in d.message

    def test_constant_assigned_and_returned_later_fails(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def judged():
    async def score(state, target):
        verdict: str = "pending"
        try:
            verdict = (await get_model(role="grader").generate("p")).completion
        except BaseException:
            verdict = ""
        return Score(value=verdict)
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert d.status == "fail"
        assert "sets verdict = ''" in d.message

    def test_constant_the_try_would_have_set_and_read_after_it_fails(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def judged():
    async def score(state, target):
        try:
            response = (await get_model(role="grader").generate("p")).completion
        except Exception:
            response = ""
        refused = "yes" in response
        return Score(value=refused)
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("fail", 12)
        assert "sets response = '', which is read after the try" in d.message

    def test_dict_holding_a_verdict_read_after_the_try_fails(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def criterion(judge, prompt):
    grading = {}
    for attempt in range(3):
        try:
            grading = parse((await judge.generate(prompt)).completion)
            break
        except Exception as e:
            if attempt == 2:
                grading = {"criteria_met": False, "explanation": str(e)}
    return {"met": grading.get("criteria_met", False)}

@scorer(metrics=[])
def rubric():
    async def score(state, target):
        return Score(value=(await criterion(get_model(role="grader"), "p"))["met"])
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("fail", 13)
        assert "sets grading = {...}" in d.message

    def test_names_the_try_does_not_set_or_nothing_reads_after_it_pass(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def judged():
    async def score(state, target):
        out = await get_model(role="grader").generate("p")
        cleaned = False
        try:
            await sandbox().exec(["rm", "-rf", "/tmp/x"])
            cleaned = True
        except Exception:
            cleaned = False
        try:
            log = await sandbox().read_file("/log")
        except Exception:
            log = ""
        return Score(value=1.0 if "yes" in out.completion else 0.0)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_a_default_left_by_an_unscored_return_or_continue_passes(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def tested():
    async def score(state, target):
        try:
            res = await sandbox().exec(["pytest"])
            passed = res.success
        except Exception:
            passed = False
            return Score.unscored(reason="scoring_failed")
        for attempt in range(3):
            try:
                out = await get_model(role="grader").generate("p")
                verdict = "yes" in out.completion
                break
            except Exception:
                verdict = False
                continue
        return Score(value=passed and verdict)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_a_none_sentinel_that_is_tested_passes(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def ask_judge(prompt):
    try:
        return (await get_model(role="grader").generate(prompt)).completion
    except Exception:
        return None

@scorer(metrics=[])
def judged():
    async def score(state, target):
        verdict = await ask_judge("p")
        if verdict is None:
            return Score.unscored(reason="grader_failed")
        retried = None
        for attempt in range(3):
            try:
                retried = (await get_model(role="grader").generate("p")).completion
            except Exception:
                retried = None
        if not retried:
            return Score.unscored(reason="grader_failed")
        return Score(value=CORRECT if "yes" in verdict + retried else INCORRECT)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_a_none_sentinel_one_caller_does_not_test_fails(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def ask_judge(prompt):
    try:
        return (await get_model(role="grader").generate(prompt)).completion
    except Exception:
        return None

@scorer(metrics=[])
def judged():
    async def score(state, target):
        first = await ask_judge("p")
        if first is None:
            return Score.unscored(reason="grader_failed")
        return Score(value=await ask_judge("q") == first)
    return score
"""
            },
        )
        assert found(results) == [("fail", "scorer.py", 10)]

    def test_none_returned_by_the_score_function_itself_passes(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def judged():
    async def score(state, target):
        try:
            out = await get_model(role="grader").generate("p")
        except Exception:
            return None
        return Score(value=out.completion)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_upper_case_parameters_and_locals_are_not_constants(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def ask(PROMPT):
    try:
        return (await get_model(role="grader").generate(PROMPT)).completion
    except Exception:
        return PROMPT

@scorer(metrics=[])
def judged():
    async def score(state, target):
        return Score(value=await ask("p"))
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_narrow_or_reraising_handlers_pass(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def judged():
    async def score(state, target):
        try:
            result = await get_model(role="grader").generate("p")
            return Score(value=parse(result.completion))
        except ValueError:
            return Score(value=INCORRECT)
        try:
            result = await get_model(role="grader").generate("p")
        except Exception as e:
            if retryable(e):
                raise
            return Score(value=INCORRECT)
        try:
            result = await get_model(role="grader").generate("p")
        except Exception as e:
            return Score.unscored(reason="grader_failed", explanation=str(e))
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_a_try_without_a_grader_or_sandbox_call_passes(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@scorer(metrics=[])
def parsed():
    async def score(state, target):
        try:
            return Score(value=float(build_judge_prompt(state.output.completion)))
        except Exception:
            return Score(value=0.0)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]


class TestSandboxFailures:
    def test_helper_returning_a_constant_warns(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def _persisted(state) -> tuple[float, str]:
    try:
        result = await sandbox().exec(["find", "/workspace"])
    except Exception:
        return 0.5, "sandbox_error"
    return 1.0, result.stdout

@scorer(metrics=[])
def persistence():
    async def score(state, target):
        value, _ = await _persisted(state)
        return Score(value=value)
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert (d.status, d.line) == ("warn", 10)
        assert "sandbox exec()/read_file() in _persisted()" in d.message
        assert "non-zero exit is the model's code failing" in (d.hint or "")

    def test_score_returned_inside_a_scorer_still_warns(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
DEFAULTS = {"accuracy": 0.0}

@scorer(metrics=[])
def tested():
    async def score(state, target):
        try:
            result = await sandbox().exec(["pytest"])
        except Exception as e:
            return Score(value=DEFAULTS.copy(), explanation=str(e))
        return Score(value=CORRECT if result.success else INCORRECT)
    return score
"""
            },
        )
        (d,) = diagnostics(results)
        assert d.status == "warn"
        assert "returns Score(value=DEFAULTS.copy())" in d.message

    def test_a_failure_the_helper_swallows_is_not_reported_again_by_its_caller(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def read_text(path):
    try:
        return await sandbox().read_file(path)
    except Exception:
        return None

async def run_bash(script):
    return await sandbox().exec(["bash", "-c", script])

async def checkpoint_one():
    try:
        text = await read_text("/out.csv")
        return parse(text)
    except Exception:
        return False

async def checkpoint_two():
    try:
        return (await run_bash("test -f /out.csv")).success
    except Exception:
        return False

@scorer(metrics=[])
def checkpoints():
    async def score(state, target):
        return Score(value=await checkpoint_one() and await checkpoint_two())
    return score
"""
            },
        )
        assert found(results) == [("warn", "scorer.py", 10), ("warn", "scorer.py", 26)]


class TestScope:
    def test_skips_without_a_scorer(self, tmp_path):
        results = run(tmp_path, {"solver.py": "def helper():\n    return 1\n"})
        assert [r.status for r in results] == ["skip"]

    def test_code_no_scorer_runs_is_not_reported(self, tmp_path):
        results = run(
            tmp_path,
            {
                "solver.py": """
async def gpu_available() -> str:
    try:
        await sandbox().exec(["nvidia-smi"])
        return "GPU"
    except Exception:
        return ""

@scorer(metrics=[])
def exact():
    async def score(state, target):
        return Score(value=state.output.completion == target.text)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_tools_are_not_followed(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
@tool
def reader():
    async def execute(path: str) -> str:
        try:
            return await sandbox().read_file(path)
        except Exception:
            return ""
    return execute

@scorer(metrics=[])
def exact():
    async def score(state, target):
        reader()
        return Score(value=1.0)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_helpers_in_other_modules_of_the_package_are_followed(self, tmp_path):
        helpers = """
async def judge_one(model, text):
    try:
        return (await model.generate(text)).completion == "yes"
    except Exception:
        return False
"""
        scorer_file = """
{imports}

@scorer(metrics=[])
def judged():
    async def score(state, target):
        return Score(value=await {call}(get_model(role="grader"), "x"))
    return score
"""
        layouts = {
            "absolute": ("from my_eval.core.helpers import judge_one", "judge_one"),
            "relative": ("from .core.helpers import judge_one as one", "one"),
            "module": ("from .core import helpers", "helpers.judge_one"),
            "re-export": ("from .core import judge_one", "judge_one"),
        }
        for layout, (imports, call) in layouts.items():
            root = tmp_path / layout
            write(root / "my_eval" / "core" / "__init__.py", "from .helpers import judge_one\n")
            results = run(
                root,
                {
                    "core/helpers.py": helpers,
                    "scorer.py": scorer_file.format(imports=imports, call=call),
                },
            )
            assert found(results) == [("fail", "helpers.py", 10)], layout

    def test_monorepo_absolute_imports_resolve_under_the_import_prefix(self, tmp_path):
        source = tmp_path / "src" / "inspect_evals"
        package = source / "my_eval"
        write(package / "__init__.py", "")
        write(
            package / "helpers.py",
            HEADER
            + """
async def judge_one(model, text):
    try:
        return (await model.generate(text)).completion == "yes"
    except Exception:
        return False
""",
        )
        write(
            package / "scorer.py",
            HEADER
            + """
from inspect_evals.my_eval.helpers import judge_one

@scorer(metrics=[])
def judged():
    async def score(state, target):
        return Score(value=await judge_one(get_model(role="grader"), "x"))
    return score
""",
        )
        ctx = context_for(package, PRESETS["monorepo"])
        assert found(list(scorer_failure_scored(ctx))) == [("fail", "helpers.py", 10)]

    def test_a_local_binding_hides_a_module_function(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
async def check(x):
    try:
        await sandbox().exec(["true"])
    except Exception:
        return False

@scorer(metrics=[])
def judged(check=None):
    async def score(state, target):
        try:
            await check(state)
        except Exception:
            return Score(value=INCORRECT)
        return Score(value=CORRECT)
    return score
"""
            },
        )
        assert [r.status for r in results] == ["pass"]

    def test_a_long_chain_of_calls_does_not_overflow(self, tmp_path):
        chain = "".join(f"async def f{i}():\n    await f{i + 1}()\n" for i in range(1500))
        results = run(
            tmp_path,
            {
                "big.py": chain
                + """
async def f1500():
    await sandbox().exec(["x"])

@scorer(metrics=[])
def deep():
    async def score(state, target):
        try:
            await f0()
        except Exception:
            return 0.0
    return score
"""
            },
        )
        assert [d.status for d in diagnostics(results)] == ["warn"]

    def test_standalone_repository_layout(self, tmp_path):
        results = run(
            tmp_path,
            {
                "scorer.py": """
from my_eval.judging import judge_one

@scorer(metrics=[])
def judged():
    async def score(state, target):
        return Score(value=await judge_one("x"))
    return score
""",
                "judging.py": """
async def judge_one(text):
    try:
        return (await get_model(role="grader").generate(text)).completion
    except Exception:
        return None
""",
            },
            config=PRESETS["single-eval"],
        )
        assert found(results) == [("fail", "judging.py", 10)]
