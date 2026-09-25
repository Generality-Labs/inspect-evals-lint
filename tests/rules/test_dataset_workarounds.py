"""Rules for dataset workarounds: deduplication and known-broken exclusions must cite their report."""

from inspect_evals_lint.rules.best_practices import (
    duplicate_filter_acknowledged,
    known_broken_reported,
)
from tests.conftest import context_for

URL = "https://huggingface.co/datasets/org/name/discussions/3"


def _run(rule, tmp_path, source: str):
    eval_dir = tmp_path / "alpha"
    eval_dir.mkdir()
    (eval_dir / "data.py").write_text(source, encoding="utf-8")
    return list(rule(context_for(eval_dir)))


class TestDuplicateFilterAcknowledged:
    def test_skips_when_nothing_is_deduplicated(self, tmp_path):
        results = _run(duplicate_filter_acknowledged, tmp_path, "ds = hf_dataset(path)\n")
        assert [r.status for r in results] == ["skip"]

    def test_passes_when_count_and_linked_reason_are_given(self, tmp_path):
        source = (
            f'a = filter_duplicate_ids(ds, max_duplicates=3, reason="3 repeats, see {URL}")\n'
            "b = filter_duplicate_ids(ds, max_duplicates=N, reason=REASON)\n"
            "c = filter_duplicate_ids(ds, **kwargs)\n"
            'd = utils.filter_duplicate_ids(ds, max_duplicates=1, reason="row 7 repeats row 3, "\n'
            f'    "{URL}")\n'
        )
        results = _run(duplicate_filter_acknowledged, tmp_path, source)
        assert [r.status for r in results] == ["pass"]
        assert "4 filter_duplicate_ids() call(s)" in results[0].message

    def test_fails_once_per_unacknowledged_call(self, tmp_path):
        source = (
            "a = filter_duplicate_ids(ds)\n"
            f'b = filter_duplicate_ids(ds, reason="see {URL}")\n'
            "c = filter_duplicate_ids(ds, max_duplicates=3)\n"
            'd = filter_duplicate_ids(ds, max_duplicates=3, reason="rows repeat")\n'
            'e = filter_duplicate_ids(ds, max_duplicates=3, reason="")\n'
        )
        results = _run(duplicate_filter_acknowledged, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [
            ("fail", 1),
            ("fail", 2),
            ("fail", 3),
            ("fail", 4),
            ("fail", 5),
        ]
        assert "max_duplicates=" in results[0].message
        assert "reason=" in results[0].message
        assert "max_duplicates=" in results[1].message
        assert "reason=" in results[2].message
        assert "URL" in results[3].message
        assert "URL" in results[4].message
        assert results[0].file.name == "data.py"


class TestKnownBrokenReported:
    def test_skips_when_nothing_is_excluded(self, tmp_path):
        results = _run(known_broken_reported, tmp_path, "ds = hf_dataset(path)\n")
        assert [r.status for r in results] == ["skip"]

    def test_passes_with_inline_dict_module_constant_or_opaque_name(self, tmp_path):
        source = (
            f'BROKEN = {{"task_100": "{URL}", "task_145": "{URL}"}}\n'
            f'a = drop_known_broken(ds, broken={{"task_001": "{URL}"}})\n'
            "b = drop_known_broken(ds, broken=BROKEN)\n"
            "c = drop_known_broken(ds, broken=imported_elsewhere)\n"
            "d = drop_known_broken(ds, **kwargs)\n"
        )
        results = _run(known_broken_reported, tmp_path, source)
        assert [r.status for r in results] == ["pass"]
        assert "4 drop_known_broken() call(s)" in results[0].message

    def test_fails_when_broken_is_missing(self, tmp_path):
        results = _run(known_broken_reported, tmp_path, "a = drop_known_broken(ds)\n")
        assert [(r.status, r.line) for r in results] == [("fail", 1)]
        assert "broken=" in results[0].message

    def test_fails_at_each_entry_without_a_report_url(self, tmp_path):
        source = (
            "BROKEN = {\n"
            f'    "task_100": "{URL}",\n'
            '    "task_145": "target not among options",\n'
            '    "task_200": "",\n'
            "}\n"
            "a = drop_known_broken(ds, broken=BROKEN)\n"
            'b = drop_known_broken(ds, broken={"task_001": "known bad"})\n'
        )
        results = _run(known_broken_reported, tmp_path, source)
        assert [(r.status, r.line) for r in results] == [
            ("fail", 3),
            ("fail", 4),
            ("fail", 7),
        ]
        assert "task_145" in results[0].message
        assert "URL" in results[0].message
        assert "task_200" in results[1].message
        assert "task_001" in results[2].message
