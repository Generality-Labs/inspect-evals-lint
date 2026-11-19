"""The sample_rng_seeded rule."""

from pathlib import Path

import pytest

from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.diagnostics import Diagnostic, Finding
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.rules.best_practices import sample_rng_seeded
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
from tests.conftest import context_for


def _run(tmp_path: Path, source: str, **others: str) -> list[Finding]:
    """Lint a package ``alpha`` holding ``data.py`` and any other modules given by name."""
    eval_dir = tmp_path / "alpha"
    eval_dir.mkdir(exist_ok=True)
    (eval_dir / "__init__.py").write_text("", encoding="utf-8")
    for name, text in {"data": source, **others}.items():
        (eval_dir / f"{name}.py").write_text(text, encoding="utf-8")
    return list(sample_rng_seeded(context_for(eval_dir)))


def _sites(results: list[Finding]) -> list[tuple[str, int | None]]:
    return [(r.file.name, r.line) for r in results if isinstance(r, Diagnostic)]


RECORD_TO_SAMPLE = """
import random


def record_to_sample(record):
    choices = record["choices"]
    random.shuffle(choices)
    return Sample(input=record["question"], choices=choices)
"""


class TestWhichCodeIsRead:
    def test_a_function_building_a_sample_warns(self, tmp_path):
        results = _run(tmp_path, RECORD_TO_SAMPLE)
        assert [r.status for r in results] == ["warn"]
        (diagnostic,) = results
        assert isinstance(diagnostic, Diagnostic)
        assert diagnostic.severity == "warning"
        assert (diagnostic.file.name, diagnostic.line) == ("data.py", 7)
        assert diagnostic.message == (
            "random.shuffle() in record_to_sample() uses the unseeded global random module"
        )
        assert diagnostic.hint is not None
        assert "random.Random(" in diagnostic.hint
        assert "comparability" in diagnostic.hint

    def test_a_helper_in_another_module_reached_from_a_task_warns(self, tmp_path):
        task = (
            "from .needles import pick\n\n\n"
            "@task\n"
            "def my_eval():\n"
            "    return Task(dataset=build(pick(rows)))\n"
        )
        needles = "import random\n\n\ndef pick(rows):\n    return random.sample(rows, 3)\n"
        results = _run(tmp_path, task, needles=needles)
        assert _sites(results) == [("needles.py", 5)]

    def test_a_solver_a_task_uses_warns_in_its_nested_solve(self, tmp_path):
        source = (
            "import random\n\n\n"
            "@solver\n"
            "def setup_tools():\n"
            "    async def solve(state, generate):\n"
            "        random.shuffle(state.tools)\n"
            "        return state\n\n"
            "    return solve\n\n\n"
            "@task\n"
            "def my_eval():\n"
            "    return Task(dataset=ds, solver=[setup_tools(), generate()])\n"
        )
        results = _run(tmp_path, source)
        assert _sites(results) == [("data.py", 7)]
        assert "in solve()" in results[0].message

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(
                "import random\n\n\ndef jitter():\n    return random.uniform(0, 1)\n",
                id="not_reached",
            ),
            pytest.param(
                "import random\n\n\n"
                "@tool\n"
                "def fake_upload():\n"
                "    async def run(name: str):\n"
                "        return random.choice(URLS)\n\n"
                "    return run\n\n\n"
                "@task\n"
                "def my_eval():\n"
                "    return Task(dataset=ds, solver=use_tools(fake_upload()))\n",
                id="tool",
            ),
            pytest.param(
                "import random\n\n\n"
                "@scorer(metrics=[mean()])\n"
                "def bootstrap():\n"
                "    async def score(state, target):\n"
                "        return Score(value=random.random())\n\n"
                "    return score\n\n\n"
                "@task\n"
                "def my_eval():\n"
                "    return Task(dataset=ds, scorer=bootstrap())\n",
                id="scorer",
            ),
            pytest.param(
                "import random\n\nSECRET = random.choices('abc', k=8)\n\n\n"
                "@task\n"
                "def my_eval():\n"
                "    return Task(dataset=ds)\n",
                id="module_level",
            ),
        ],
    )
    def test_other_code_is_not_read(self, tmp_path, source):
        assert [r.status for r in _run(tmp_path, source)] == ["pass"]


class TestTheGlobalModule:
    def test_one_diagnostic_per_function_names_the_other_lines(self, tmp_path):
        source = (
            "import random\n\n\n"
            "def build(rows):\n"
            "    picked = random.sample(rows, 3)\n"
            "    random.shuffle(picked)\n"
            "    return MemoryDataset([Sample(input=r) for r in picked])\n"
        )
        results = _run(tmp_path, source)
        assert _sites(results) == [("data.py", 5)]
        assert results[0].message.endswith("; more at line(s) 6")

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(
                "import random\n\n\n"
                "def build(rows, seed=42):\n"
                "    random.seed(seed)\n"
                "    random.shuffle(rows)\n"
                "    return MemoryDataset(rows)\n",
                id="in_the_function",
            ),
            pytest.param(
                "import random\n\nrandom.seed(42)\n\n\n"
                "def build(rows):\n"
                "    random.shuffle(rows)\n"
                "    return MemoryDataset(rows)\n",
                id="at_module_level",
            ),
            pytest.param(
                "import random\n\n\n"
                "def solver():\n"
                "    random.seed(42)\n\n"
                "    async def solve(state, generate):\n"
                "        random.shuffle(state.tools)\n"
                "        return Sample(input='x')\n\n"
                "    return solve\n",
                id="in_an_enclosing_function",
            ),
        ],
    )
    def test_a_seed_call_passes(self, tmp_path, source):
        assert [r.status for r in _run(tmp_path, source)] == ["pass"]

    def test_a_seed_call_without_an_argument_does_not_count(self, tmp_path):
        source = (
            "import random\n\n\n"
            "def build(rows):\n"
            "    random.seed()\n"
            "    random.shuffle(rows)\n"
            "    return MemoryDataset(rows)\n"
        )
        assert _sites(_run(tmp_path, source)) == [("data.py", 6)]

    def test_a_seed_call_in_another_function_does_not_count(self, tmp_path):
        source = (
            "import random\n\n\n"
            "def unrelated():\n"
            "    random.seed(1)\n\n\n"
            "def build(rows):\n"
            "    random.shuffle(rows)\n"
            "    return MemoryDataset(rows)\n"
        )
        assert _sites(_run(tmp_path, source)) == [("data.py", 9)]

    def test_names_imported_from_random(self, tmp_path):
        source = (
            "from random import shuffle\n\n\n"
            "def build(rows):\n"
            "    shuffle(rows)\n"
            "    return MemoryDataset(rows)\n"
        )
        results = _run(tmp_path, source)
        assert _sites(results) == [("data.py", 5)]
        assert results[0].message.startswith("shuffle() in build()")

    def test_an_aliased_module(self, tmp_path):
        source = (
            "import random as rnd\n\n\n"
            "def build(rows):\n"
            "    rnd.shuffle(rows)\n"
            "    return MemoryDataset(rows)\n"
        )
        assert _sites(_run(tmp_path, source)) == [("data.py", 5)]

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(
                "from random import Random\n\nrandom = Random(42)\n\n\n"
                "def build(rows):\n"
                "    random.shuffle(rows)\n"
                "    return MemoryDataset(rows)\n",
                id="a_seeded_instance_named_random",
            ),
            pytest.param(
                "def build(rows, random):\n"
                "    random.shuffle(rows)\n"
                "    return MemoryDataset(rows)\n",
                id="a_parameter_named_random",
            ),
            pytest.param(
                "import numpy as np\n\n\n"
                "def build(rows):\n"
                "    np.random.shuffle(rows)\n"
                "    return MemoryDataset(rows)\n",
                id="legacy_numpy_is_ruffs",
            ),
            pytest.param(
                "import random\n\n\n"
                "def build(rows):\n"
                "    state = random.getstate()\n"
                "    return MemoryDataset(rows)\n",
                id="not_a_draw",
            ),
        ],
    )
    def test_what_is_not_the_global_module(self, tmp_path, source):
        assert [r.status for r in _run(tmp_path, source)] == ["pass"]


class TestGenerators:
    @pytest.mark.parametrize(
        ("constructor", "prelude"),
        [
            ("random.Random()", "import random"),
            ("Random()", "from random import Random"),
            ("random.Random(None)", "import random"),
            ("np.random.default_rng()", "import numpy as np"),
            ("default_rng(seed=None)", "from numpy.random import default_rng"),
        ],
    )
    def test_a_generator_built_without_a_seed_warns(self, tmp_path, constructor, prelude):
        source = (
            f"{prelude}\n\n\n"
            "def build(rows):\n"
            f"    rng = {constructor}\n"
            "    rng.shuffle(rows)\n"
            "    return MemoryDataset(rows)\n"
        )
        results = _run(tmp_path, source)
        assert _sites(results) == [("data.py", 5)]
        name = constructor.split("(")[0]
        assert results[0].message == f"{name}() in build() is built without a seed"

    @pytest.mark.parametrize(
        "constructor",
        [
            "random.Random(42)",
            "random.Random(sample_id)",
            "random.Random(x=SEED)",
            "np.random.default_rng(seed)",
            "np.random.default_rng(seed=1234)",
            "random.SystemRandom()",
        ],
    )
    def test_a_generator_given_a_seed_passes(self, tmp_path, constructor):
        source = (
            "import random\n\nSEED = 7\n\n\n"
            "def build(rows, sample_id, seed=0):\n"
            f"    rng = {constructor}\n"
            "    rng.shuffle(rows)\n"
            "    return MemoryDataset(rows)\n"
        )
        assert [r.status for r in _run(tmp_path, source)] == ["pass"]

    def test_a_seed_that_defaults_to_none_warns_at_the_constructor(self, tmp_path):
        source = (
            "import random\n\n\n"
            "def build(rows, seed=None):\n"
            "    rng = random.Random(seed)\n"
            "    rng.shuffle(rows)\n"
            "    return MemoryDataset(rows)\n"
        )
        results = _run(tmp_path, source)
        assert _sites(results) == [("data.py", 5)]
        assert results[0].message == (
            "random.Random() in build() is given seed, which defaults to None"
        )

    def test_a_module_level_generator_a_read_function_uses_warns(self, tmp_path):
        source = (
            "from random import Random\n\n"
            "rng = Random()\n"
            "unused = Random()\n\n\n"
            "def record_to_sample(record):\n"
            "    rng.shuffle(record['choices'])\n"
            "    return Sample(input=record['question'], choices=record['choices'])\n"
        )
        results = _run(tmp_path, source)
        assert _sites(results) == [("data.py", 3)]
        assert results[0].message == (
            "Random() bound to rng is built without a seed and used in record_to_sample()"
        )

    def test_a_module_level_generator_named_random(self, tmp_path):
        source = (
            "from random import Random\n\n"
            "random = Random()\n\n\n"
            "def record_to_sample(record):\n"
            "    random.shuffle(record['choices'])\n"
            "    return Sample(input=record['question'], choices=record['choices'])\n"
        )
        assert _sites(_run(tmp_path, source)) == [("data.py", 3)]

    def test_a_module_level_generator_only_other_code_uses_passes(self, tmp_path):
        source = (
            "from random import Random\n\nrng = Random()\n\n\n"
            "def jitter():\n"
            "    return rng.random()\n\n\n"
            "def build(rows):\n"
            "    return MemoryDataset(rows)\n"
        )
        assert [r.status for r in _run(tmp_path, source)] == ["pass"]


def test_unparsable_file_is_reported(tmp_path):
    results = _run(tmp_path, RECORD_TO_SAMPLE, broken="def f(:\n")
    assert sorted(r.status for r in results) == ["fail", "warn"]


def test_line_suppression(tmp_path):
    source = (
        "import random\n\n\n"
        "def record_to_sample(record):\n"
        "    port = random.randint(0, 999)  # inspect-evals-lint: ignore[sample_rng_seeded] -- a debug port, never shown to the model\n"
        "    return Sample(input=record['question'], metadata={'port': port})\n"
    )
    results = _run(tmp_path, source)
    for r in results:
        r.rule = get_rule("sample_rng_seeded")
    apply_suppressions(
        results, load_suppressions(context_for(tmp_path / "alpha")), PRESETS["multi-eval"], tmp_path
    )
    assert [(r.status, r.line) for r in results] == [("suppressed", 5)]
