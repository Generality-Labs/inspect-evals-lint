"""Rules for seeded shuffles: shuffle_choices is a seed or off, and a shuffling loader has a seed."""

from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.rules.best_practices import shuffle_choices_seeded, shuffle_seeded
from inspect_evals_lint.suppressions import apply_suppressions, load_suppressions
from tests.conftest import context_for


def _run(rule, tmp_path, source: str):
    eval_dir = tmp_path / "alpha"
    eval_dir.mkdir()
    (eval_dir / "data.py").write_text(source, encoding="utf-8")
    return list(rule(context_for(eval_dir)))


class TestShuffleChoicesSeeded:
    def test_skips_when_no_call_passes_shuffle_choices(self, tmp_path):
        results = _run(shuffle_choices_seeded, tmp_path, 'ds = hf_dataset("p", split="t")\n')
        assert [r.status for r in results] == ["skip"]

    def test_passes_a_seed_off_or_unknown_value(self, tmp_path):
        source = (
            "SEED = 42\n"
            'a = hf_dataset("p", split="t", shuffle_choices=7)\n'
            'b = hf_dataset("p", split="t", shuffle_choices=1)\n'
            'c = csv_dataset("f", shuffle_choices=False)\n'
            'd = json_dataset("f", shuffle_choices=None)\n'
            'e = json_dataset("f", shuffle_choices=SEED)\n'
            'f = json_dataset("f", shuffle_choices=settings.shuffle)\n'
            "def g(shuffle_choices=True):\n"
            "    if not seeded:\n"
            "        shuffle_choices = 3\n"
            '    return hf_dataset("p", split="t", shuffle_choices=shuffle_choices)\n'
            "def h(shuffle_choices):\n"
            '    return hf_dataset("p", split="t", shuffle_choices=shuffle_choices)\n'
        )
        results = _run(shuffle_choices_seeded, tmp_path, source)
        assert [r.status for r in results] == ["pass"]
        assert "8 call(s)" in results[0].message

    def test_fails_a_literal_true_at_the_call(self, tmp_path):
        results = _run(
            shuffle_choices_seeded,
            tmp_path,
            'ds = hf_dataset("p", split="t", shuffle_choices=True)\n',
        )
        assert [(r.status, r.line) for r in results] == [("fail", 1)]
        assert results[0].message == (
            "hf_dataset(shuffle_choices=True) shuffles the choices without a seed"
        )
        assert "DEFAULT_SHUFFLE_SEED" in results[0].hint
        assert "comparability" in results[0].hint

    def test_fails_a_module_constant_at_the_call(self, tmp_path):
        source = 'SHUFFLE = True\nds = csv_dataset("f", shuffle_choices=SHUFFLE)\n'
        results = _run(shuffle_choices_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("fail", 2)]

    def test_a_constant_bound_twice_is_unknown(self, tmp_path):
        source = (
            "SHUFFLE = True\n"
            "if seeded:\n"
            "    SHUFFLE = 42\n"
            'ds = csv_dataset("f", shuffle_choices=SHUFFLE)\n'
        )
        results = _run(shuffle_choices_seeded, tmp_path, source)
        assert [r.status for r in results] == ["pass"]

    def test_points_at_the_parameter_default_and_reports_it_once(self, tmp_path):
        source = (
            "@task\n"
            "def my_eval(\n"
            "    shuffle_choices: bool = True,\n"
            "):\n"
            '    test = hf_dataset("p", split="test", shuffle_choices=shuffle_choices)\n'
            '    dev = hf_dataset("p", split="dev", shuffle_choices=shuffle_choices)\n'
            "    return Task(dataset=test)\n"
        )
        results = _run(shuffle_choices_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("fail", 3)]
        assert results[0].message == (
            "shuffle_choices defaults to True, and hf_dataset() on line 5 shuffles the choices "
            "without a seed"
        )

    def test_follows_a_default_that_names_a_constant(self, tmp_path):
        source = (
            "UNSEEDED = True\n"
            "SEEDED = 42\n"
            "def a(shuffle_choices=UNSEEDED):\n"
            '    return hf_dataset("p", split="t", shuffle_choices=shuffle_choices)\n'
            "def b(shuffle_choices=SEEDED):\n"
            '    return hf_dataset("p", split="t", shuffle_choices=shuffle_choices)\n'
        )
        results = _run(shuffle_choices_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("fail", 3)]

    def test_reads_an_enclosing_functions_default(self, tmp_path):
        source = (
            "def my_eval(shuffle_choices=True):\n"
            "    def load():\n"
            '        return hf_dataset("p", split="t", shuffle_choices=shuffle_choices)\n'
            "    return load()\n"
        )
        results = _run(shuffle_choices_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("fail", 1)]

    def test_reads_any_call_passing_shuffle_choices(self, tmp_path):
        source = (
            "def my_eval(shuffle_choices: bool = True):\n"
            "    return Task(dataset=get_dataset(shuffle_choices=shuffle_choices))\n"
        )
        results = _run(shuffle_choices_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("fail", 1)]
        assert "get_dataset() on line 2" in results[0].message

    def test_a_suppression_beside_the_default_applies(self, tmp_path):
        source = (
            "def my_eval(\n"
            "    shuffle_choices: bool = True,  # inspect-evals-lint: ignore[shuffle_choices_seeded] -- matches the paper\n"
            "):\n"
            '    return hf_dataset("p", split="t", shuffle_choices=shuffle_choices)\n'
        )
        results = _run(shuffle_choices_seeded, tmp_path, source)
        for r in results:
            r.rule = get_rule("shuffle_choices_seeded")
        apply_suppressions(
            results,
            load_suppressions(context_for(tmp_path / "alpha")),
            PRESETS["multi-eval"],
            tmp_path,
        )
        assert [(r.status, r.line) for r in results] == [("suppressed", 2)]


class TestShuffleSeeded:
    def test_skips_when_no_call_passes_shuffle(self, tmp_path):
        results = _run(shuffle_seeded, tmp_path, 'ds = hf_dataset("p", split="t")\n')
        assert [r.status for r in results] == ["skip"]

    def test_passes_a_seeded_off_or_unknown_shuffle(self, tmp_path):
        source = (
            'a = hf_dataset("p", split="t", shuffle=True, seed=42)\n'
            'b = hf_dataset("p", split="t", shuffle=False)\n'
            'c = hf_dataset("p", split="t", shuffle=flag)\n'
            'd = hf_dataset("p", split="t", shuffle=True, seed=config.seed)\n'
            'e = hf_dataset("p", split="t", shuffle=True, **options)\n'
            "def f(seed: int | None = None):\n"
            "    seed = seed or 7\n"
            '    return hf_dataset("p", split="t", shuffle=True, seed=seed)\n'
            "def g(seed: int | None = 3):\n"
            '    return hf_dataset("p", split="t", shuffle=True, seed=seed)\n'
        )
        results = _run(shuffle_seeded, tmp_path, source)
        assert [r.status for r in results] == ["pass"]
        assert "7 call(s)" in results[0].message

    def test_warns_on_a_shuffle_without_a_seed(self, tmp_path):
        source = (
            'a = hf_dataset("p", split="t", shuffle=True)\n'
            'b = hf_dataset("p", split="t", shuffle=True, seed=None)\n'
        )
        results = _run(shuffle_seeded, tmp_path, source)
        assert [(r.status, r.severity, r.line) for r in results] == [
            ("warn", "warning", 1),
            ("warn", "warning", 2),
        ]
        assert results[0].message == "hf_dataset(shuffle=True) shuffles the samples without a seed"
        assert "--sample-shuffle" in results[0].hint

    def test_warns_when_the_seed_defaults_to_none(self, tmp_path):
        source = (
            "def load(seed: int | None = None):\n"
            '    return hf_dataset("p", split="t", shuffle=True, seed=seed)\n'
        )
        results = _run(shuffle_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("warn", 2)]

    def test_points_at_the_shuffle_default(self, tmp_path):
        source = (
            "@task\n"
            "def my_eval(shuffle: bool = True):\n"
            "    return Task(dataset=get_dataset(shuffle=shuffle))\n"
        )
        results = _run(shuffle_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("warn", 2)]
        assert results[0].message == (
            "shuffle defaults to True, and get_dataset() on line 3 shuffles the samples without "
            "a seed"
        )

    def test_reads_shuffle_and_seed_passed_positionally_to_known_loaders(self, tmp_path):
        source = (
            'a = hf_dataset("p", "t", None, None, "rev", fields, False, True)\n'
            'b = hf_dataset("p", "t", None, None, "rev", fields, False, True, 42)\n'
            'c = load_csv_dataset("f", "alpha", fields, False, True)\n'
            'd = csv_dataset("f", fields, False, True, 42)\n'
        )
        results = _run(shuffle_seeded, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [("warn", 1), ("warn", 3)]
