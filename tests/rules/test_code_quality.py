"""Code-quality rules."""

from inspect_evals_lint.config import PRESETS
from inspect_evals_lint.rules.code_quality import unscored_reason
from tests.conftest import context_for


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

        results = self._run(
            tmp_path, "a = Score.unscored()  # inspect-evals-lint: ignore[IECQ003]\n"
        )
        for r in results:
            r.rule = get_rule("unscored_reason")
        apply_suppressions(
            results,
            load_suppressions(context_for(tmp_path / "alpha")),
            PRESETS["template"],
            tmp_path,
        )
        assert [r.status for r in results] == ["suppressed"]
