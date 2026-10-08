"""metric_epoch_safety: custom metrics read scores as the epoch reducer leaves them and do not modify them."""

from inspect_evals_lint.rules.metrics import metric_epoch_safety
from tests.conftest import context_for

HEADER = "from inspect_ai.scorer import Metric, SampleScore, Score, metric, score_reducer\n\n\n"


def _run(tmp_path, source: str):
    eval_dir = tmp_path / "alpha"
    eval_dir.mkdir()
    (eval_dir / "metrics.py").write_text(HEADER + source, encoding="utf-8")
    return list(metric_epoch_safety(context_for(eval_dir)))


def _lines(results) -> list[tuple[str, int | None]]:
    # Line numbers are relative to the source after HEADER.
    return [(r.status, r.line - 3 if r.line else None) for r in results]


def _metric(body: str, decorator: str = "@metric") -> str:
    """A metric factory whose inner ``metric(scores)`` has ``body`` (four-space indented lines)."""
    return (
        f"{decorator}\n"
        "def m() -> Metric:\n"
        "    def metric(scores: list[SampleScore]) -> float:\n"
        f"{body}"
        "    return metric\n"
    )


def test_skips_when_there_are_no_metrics_or_reducers(tmp_path):
    results = _run(tmp_path, "def helper(scores):\n    return scores[0].score.as_int()\n")
    assert [r.status for r in results] == ["skip"]


def test_passes_metrics_that_read_reduced_scores_safely(tmp_path):
    source = _metric(
        "        values = [s.score.as_float() for s in scores]\n"
        "        n = int(len(values))\n"
        "        hits = int(sum(s.score.value == 1 for s in scores))\n"
        "        ok = [v for v in values if isinstance(v, (int, float)) or isinstance(v, int | float)]\n"
        "        nan = [s for s in scores if isinstance(s.score.value, float) and math.isnan(s.score.value)]\n"
        '        labels = [s.score.metadata["label"] for s in scores]\n'
        '        groups = [s.sample_metadata["group"] for s in scores]\n'
        "        fresh = [s.model_copy(update={'score': Score(value=0)}) for s in scores]\n"
        "        return sum(values) / n\n"
    )
    results = _run(tmp_path, source)
    assert [r.status for r in results] == ["pass"]
    assert "1 custom metric(s)" in results[0].message


def test_fails_on_as_int_and_as_bool(tmp_path):
    source = _metric(
        "        a = [s.score.as_int() for s in scores]\n"
        "        b = [s.score.as_bool() for s in scores]\n"
        "        return 0.0\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 4), ("fail", 5)]
    assert ".as_int()" in results[0].message
    assert ".as_bool()" in results[1].message
    assert results[0].hint is not None
    assert "as_float()" in results[0].hint


def test_fails_on_int_of_anything_computed_from_a_score_value(tmp_path):
    source = _metric(
        "        total = int(sum(cast(dict, s.score.value)['n'] for s in scores))\n"
        "        running = 0\n"
        "        for s in scores:\n"
        "            value = cast(dict[str, Any], s.score.value)\n"
        "            kept = int(value['kept'])\n"
        "            dropped = int(value['n'] - value['kept'])\n"
        "            already_narrowed = int(kept - dropped)\n"
        "            running += s.score.value['n']\n"
        "        last = int(running)\n"
        "        return 0.0\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 4), ("fail", 8), ("fail", 9), ("fail", 12)]
    assert all("int()" in r.message for r in results)


def test_values_collected_into_a_list_are_still_score_values(tmp_path):
    source = _metric(
        "        values = []\n"
        "        for s in scores:\n"
        "            values.append(s.score.value['n'])\n"
        "        return float(int(sum(values)))\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 7)]


def test_only_the_unpacked_name_that_carries_the_value_is_one(tmp_path):
    source = _metric(
        "        out = {}\n"
        "        for s in scores:\n"
        "            for k, v in s.score.value.items():\n"
        "                out[int(k)] = int(v)\n"
        "        vals = [s.score.value for s in scores]\n"
        "        for i, v in enumerate(vals):\n"
        "            out[int(i)] = int(v)\n"
        "        for n, v in zip(names, vals):\n"
        "            out[int(n)] = int(v)\n"
        "        a, b = 3, scores[0].score.value\n"
        "        return float(int(a) + int(b))\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [
        ("fail", 7),
        ("fail", 10),
        ("fail", 12),
        ("fail", 14),
    ]
    # The int() of the value, not of the key, index or name beside it.
    assert [r.column for r in results] == [31, 27, 27, 31]


def test_warns_on_single_type_isinstance_of_a_score_value(tmp_path):
    source = _metric(
        "        if not all(isinstance(s.score.value, int) for s in scores):\n"
        "            raise ValueError()\n"
        "        for s in scores:\n"
        "            value = s.score.value\n"
        "            if not isinstance(value['score'], float):\n"
        "                continue\n"
        "        dicts = [s.score.as_dict() for s in scores]\n"
        "        floats = [d for d in dicts if all(isinstance(v, float) for v in d.values())]\n"
        "        return 0.0\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("warn", 4), ("warn", 8)]
    assert "isinstance(..., int)" in results[0].message
    assert "isinstance(..., float)" in results[1].message
    assert all(r.severity == "warning" for r in results)


def test_fails_on_reading_a_field_the_reducer_drops(tmp_path):
    source = _metric(
        '        wins = sum(1 for s in scores if s.score.answer == "win")\n'
        "        why = [s.score.explanation for s in scores]\n"
        "        scorable = [s for s in scores if not s.score.reason]\n"
        "        unrelated = [result.answer for result in other]\n"
        "        return wins / len(scores)\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 4), ("fail", 5), ("fail", 6)]
    assert "Score.answer" in results[0].message
    assert "Score.explanation" in results[1].message
    assert "Score.reason" in results[2].message


def test_checks_helpers_nested_in_the_metric(tmp_path):
    source = (
        "@metric\n"
        "def m() -> Metric:\n"
        "    def _stderr(scores: list[SampleScore]) -> float:\n"
        "        return float(int(scores[0].score.value['n']))\n"
        "\n"
        "    def metric(scores: list[SampleScore]) -> float:\n"
        "        return _stderr(scores)\n"
        "\n"
        "    return metric\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 4)]


def test_unreduced_metrics_and_reducers_may_read_raw_epoch_scores(tmp_path):
    source = (
        _metric(
            '        return sum(s.score.as_int() for s in scores if s.score.answer == "w")\n',
            decorator='@metric(scores="unreduced")',
        )
        + "\n\n"
        "@score_reducer\n"
        "def r():\n"
        "    def reduce(scores: list[Score]) -> Score:\n"
        "        return Score(value=max(int(s.value) for s in scores), answer=scores[0].answer)\n"
        "\n"
        "    return reduce\n"
    )
    results = _run(tmp_path, source)
    assert [r.status for r in results] == ["pass"]
    assert "2 custom metric(s)" in results[0].message


def test_fails_on_writing_to_the_scores_a_metric_was_handed(tmp_path):
    source = _metric(
        "        cleaned = [s for s in scores if s.score.value != 'INVALID']\n"
        "        if not cleaned:\n"
        "            cleaned = scores\n"
        "            for sample_score in cleaned:\n"
        "                sample_score.score.value = 0\n"
        "        for i, s in enumerate(scores):\n"
        "            s.score.metadata['seen'] = True\n"
        "        scores[0] = None\n"
        "        return 0.0\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 8), ("fail", 10), ("fail", 11)]
    assert results[0].message == (
        "metric writes to the scores it was handed: sample_score.score.value"
    )


def test_writing_to_new_objects_or_local_containers_is_allowed(tmp_path):
    source = (
        _metric(
            "        best = {}\n"
            "        for s in scores:\n"
            "            name = s.sample_metadata['task']\n"
            "            best[name] = 1\n"
            "            best.setdefault(name, 0)\n"
            "        copies = [s.model_copy() for s in scores]\n"
            "        copies[0].score = None\n"
            "        return 0.0\n"
        )
        + "\n\n"
        "@score_reducer\n"
        "def r():\n"
        "    def reduce(scores: list[Score]) -> Score:\n"
        "        reduced = base(scores)\n"
        "        reduced.metadata = {**(reduced.metadata or {}), 'attempts': []}\n"
        "        return reduced\n"
        "\n"
        "    return reduce\n"
    )
    results = _run(tmp_path, source)
    assert [r.status for r in results] == ["pass"]


def test_fails_on_a_reducer_or_unreduced_metric_writing_to_its_input(tmp_path):
    source = (
        "@score_reducer\n"
        "def r():\n"
        "    def reduce(scores: list[Score]) -> Score:\n"
        "        scores[0].value = 1\n"
        "        return scores[0]\n"
        "\n"
        "    return reduce\n"
        "\n\n"
        + _metric(
            "        scores[0].score.answer = 'x'\n        return 0.0\n",
            decorator='@metric(scores="unreduced")',
        )
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 4), ("fail", 13)]
    assert results[0].message.startswith("reducer writes")
    assert results[1].message.startswith("metric writes")


def test_rebinding_to_a_deep_copy_ends_the_alias(tmp_path):
    source = _metric(
        "        for s in scores:\n"
        "            s = s.model_copy(deep=True)\n"
        "            s.score.value = 0\n"
        "        for s in scores:\n"
        "            s = copy.deepcopy(s)\n"
        "            s.score.metadata['k'] = 1\n"
        "        fresh = [copy.deepcopy(s) for s in scores]\n"
        "        fresh[0].score.value = 0\n"
        "        first = scores[0]\n"
        "        first = make_score()\n"
        "        first.score.value = 0\n"
        "        return 0.0\n"
    )
    results = _run(tmp_path, source)
    assert [r.status for r in results] == ["pass"]


def test_a_copy_on_only_one_branch_still_writes_to_the_original(tmp_path):
    source = _metric(
        "        for s in scores:\n"
        "            if s.score.value == 'INVALID':\n"
        "                s = copy.deepcopy(s)\n"
        "            s.score.value = 0\n"
        "        return 0.0\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 7)]


def test_a_shallow_copy_shares_the_objects_it_holds(tmp_path):
    source = _metric(
        "        c = scores[0].model_copy()\n"
        "        c.score = None\n"
        "        c.score.value = 0\n"
        "        d = copy.copy(scores[0])\n"
        "        d.score.metadata['k'] = 1\n"
        "        e = scores[0].model_copy(update={'score': Score(value=0)})\n"
        "        e.score.value = 1\n"
        "        f = scores[0].model_copy(update=changes)\n"
        "        f.score.value = 1\n"
        "        kept = sorted(scores, key=key)\n"
        "        kept[0] = None\n"
        "        kept[0].score.value = 0\n"
        "        return 0.0\n"
    )
    results = _run(tmp_path, source)
    assert _lines(results) == [("fail", 6), ("fail", 8), ("fail", 15)]
