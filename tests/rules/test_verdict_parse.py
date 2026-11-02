"""The verdict_parse_anchored rule."""

from pathlib import Path

import pytest

from inspect_evals_lint.config import LintConfig
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding
from inspect_evals_lint.rules.verdict_parse import verdict_parse_anchored
from tests.conftest import make_monorepo, make_register_repo, write


def _run(root: Path, config: LintConfig, source: str, name: str = "alpha") -> list[Finding]:
    write(config.package_dir(root, name) / "judge.py", source)
    return list(verdict_parse_anchored(LintContext.build(root, name, config)))


def _diagnostics(results: list[Finding]) -> list[Diagnostic]:
    return [r for r in results if isinstance(r, Diagnostic)]


def _statuses(results: list[Finding]) -> list[str]:
    return [r.status for r in results]


class TestSubstringVerdicts:
    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('refusal = "yes" in response\n', id="bare_token"),
            pytest.param('ok = "no" not in text.lower()\n', id="not_in"),
            pytest.param('ok = "YES" in answer.upper()\n', id="upper_case_token"),
            pytest.param('ok = "answer>yes" in completion_lower\n', id="label_then_tag_close"),
            pytest.param('ok = "answer: no" in completion_lower\n', id="label_then_colon"),
            pytest.param('ok = "Correct" in grade\n', id="title_case_token"),
            pytest.param('ok = "pass" in judge_output\n', id="pass_token"),
            pytest.param(
                'def parse(self, answer):\n    if "yes" in answer:\n        return True\n',
                id="inside_a_function",
            ),
        ],
    )
    def test_a_verdict_token_tested_by_containment_fails(self, tmp_path, source):
        results = _run(tmp_path, make_monorepo(tmp_path), source)
        assert _statuses(results) == ["fail"]
        (diagnostic,) = _diagnostics(results)
        assert diagnostic.severity == "error"
        assert diagnostic.file.name == "judge.py"
        assert diagnostic.hint is not None
        assert "label" in diagnostic.hint

    def test_the_message_names_the_token(self, tmp_path):
        results = _run(tmp_path, make_monorepo(tmp_path), 'refusal = "yes" in response\n')
        (diagnostic,) = _diagnostics(results)
        assert '"yes"' in diagnostic.message
        assert diagnostic.line == 1

    def test_two_tokens_on_one_line_give_two_diagnostics(self, tmp_path):
        source = 'ok = "yes" in text and "no" not in text\n'
        results = _run(tmp_path, make_monorepo(tmp_path), source)
        assert [d.line for d in _diagnostics(results)] == [1, 1]
        assert [d.column for d in _diagnostics(results)] == [6, 24]

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('ok = "yes" in ("yes", "no")\n', id="literal_tuple"),
            pytest.param('ok = "yes" in ["yes", "no"]\n', id="literal_list"),
            pytest.param('ok = "yes" in {"yes": 1, "no": 0}\n', id="literal_dict"),
            pytest.param('ok = answer == "yes"\n', id="equality"),
            pytest.param(
                'ok = answer.strip().lower() in ("yes", "no")\n', id="token_set_membership"
            ),
            pytest.param('ok = answer.startswith("yes")\n', id="startswith"),
            pytest.param('ok = "malicious" in response\n', id="task_word_not_in_vocabulary"),
            pytest.param('ok = "yes or no" in prompt\n', id="phrase_not_a_token"),
            pytest.param(
                'def score(state, target):\n    return "true" in state.output.completion.lower()\n',
                id="model_under_test_output_inline",
            ),
            pytest.param(
                "def score(state, target):\n"
                "    pred = state.output.completion\n"
                '    return "true" in pred.lower()\n',
                id="model_under_test_output_through_a_name",
            ),
            pytest.param(
                "def score(state, target):\n"
                "    pred = state.output.completion.strip().lower()\n"
                '    return "false" in pred\n',
                id="model_under_test_output_through_a_chained_name",
            ),
        ],
    )
    def test_other_containment_tests_pass(self, tmp_path, source):
        results = _run(tmp_path, make_monorepo(tmp_path), source)
        assert _statuses(results) == ["pass"]

    def test_a_grader_reply_in_the_same_function_is_still_flagged(self, tmp_path):
        source = (
            "async def score(state, target):\n"
            "    pred = state.output.completion\n"
            "    result = await grader.generate(pred)\n"
            '    return "yes" in result.completion.lower()\n'
        )
        results = _run(tmp_path, make_monorepo(tmp_path), source)
        assert _statuses(results) == ["fail"]

    def test_the_container_name_is_not_tested_when_the_token_is_the_left_operand(self, tmp_path):
        # ``x in "yes"`` reverses the operands; it tests whether x is inside the word.
        results = _run(tmp_path, make_monorepo(tmp_path), 'ok = answer in "yes"\n')
        assert _statuses(results) == ["pass"]


class TestUnanchoredRegexVerdicts:
    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('m = re.search(r"y", grade, re.IGNORECASE)\n', id="one_letter"),
            pytest.param('m = re.search(r"INCORRECT", text)\n', id="one_word"),
            pytest.param(
                'm = re.search(r"correct: (yes|no)", text)\n', id="label_then_alternation"
            ),
            pytest.param('m = re.search(r"[01]", text)\n', id="one_character_class"),
            pytest.param('m = re.search(r"(A|B|C)", raw_grade)\n', id="one_character_alternation"),
            pytest.param('m = re.findall(r"(?:yes|no)", text)\n', id="non_capturing_alternation"),
            pytest.param('m = re.search(r"(?P<verdict>true|false)", text)\n', id="named_group"),
            pytest.param('m = re.match("yes", text)\n', id="plain_string_pattern"),
            pytest.param('m = re.finditer(r"(yes|no)", text)\n', id="finditer"),
            pytest.param(
                'm = re.fullmatch(r"\\s*(yes|no)\\s*", text)\n', id="fullmatch_is_not_an_anchor"
            ),
            pytest.param('m = re.search(pattern=r"(yes|no)", string=text)\n', id="pattern_keyword"),
            pytest.param('m = re.search(r"(?i)correct:\\s*(yes|no)", text)\n', id="inline_flag"),
        ],
    )
    def test_an_unanchored_verdict_pattern_fails(self, tmp_path, source):
        results = _run(tmp_path, make_monorepo(tmp_path), "import re\n" + source)
        assert _statuses(results) == ["fail"]
        (diagnostic,) = _diagnostics(results)
        assert diagnostic.severity == "error"
        assert diagnostic.line == 2

    def test_a_compiled_pattern_is_flagged_once_at_the_compile_site(self, tmp_path):
        source = (
            "import re\n"
            'VERDICT = re.compile(r"(yes|no)")\n'
            "\n"
            "def parse(text):\n"
            "    return VERDICT.search(text)\n"
        )
        results = _run(tmp_path, make_monorepo(tmp_path), source)
        assert [d.line for d in _diagnostics(results)] == [2]

    def test_the_message_quotes_the_pattern(self, tmp_path):
        source = 'import re\nm = re.search(r"correct: (yes|no)", text)\n'
        results = _run(tmp_path, make_monorepo(tmp_path), source)
        (diagnostic,) = _diagnostics(results)
        assert "correct: (yes|no)" in diagnostic.message
        assert "re.search()" in diagnostic.message

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('m = re.search(r"\\b(yes|no)\\b", text)\n', id="word_boundary"),
            pytest.param(
                'm = re.search(r"^(?:[-*]\\s*)?correct:\\s*(yes|no)", text, re.M)\n',
                id="line_start",
            ),
            pytest.param('m = re.search(r"(?m)^ANSWER:\\s*([01])", text)\n', id="inline_multiline"),
            pytest.param('m = re.search(r"(?i)correct:\\s*(yes|no)\\s*$", text)\n', id="line_end"),
            pytest.param('m = re.search(r"\\A(yes|no)", text)\n', id="string_start"),
            pytest.param('m = re.search(r"(yes|no)(?!\\w)", text)\n', id="lookahead_boundary"),
            pytest.param('m = re.search(r"(?<!\\w)(yes|no)", text)\n', id="lookbehind_boundary"),
            pytest.param('m = re.search(rf"\\b{token}\\b", grade)\n', id="f_string_with_boundary"),
            pytest.param(
                'm = re.search(r"<answer>(.*?)</answer>", text)\n', id="tag_capture_no_token"
            ),
            pytest.param(
                'm = re.search(r"answer\\s*(?:>|:)\\s*(\\S+)", text)\n', id="label_then_any_word"
            ),
            pytest.param('m = re.search(r"[0-9]+", text)\n', id="quantified_class"),
            pytest.param('m = re.search(r"[A-D]", text)\n', id="range_class"),
            pytest.param('m = re.search(r"\\d+\\.\\d+", text)\n', id="number_only"),
            pytest.param('m = re.sub(r"(yes|no)", "", text)\n', id="sub_is_not_a_read"),
            pytest.param(
                "m = re.search(VERDICT_PATTERN, text)\n", id="pattern_from_an_unknown_name"
            ),
            pytest.param(
                'm = re.search(r"(yes|no|maybe)", text)\n', id="alternation_with_a_non_verdict_word"
            ),
            pytest.param(
                'm = re.search(r"yes.*no", text)\n', id="tokens_with_other_content_between"
            ),
        ],
    )
    def test_anchored_or_non_verdict_patterns_pass(self, tmp_path, source):
        results = _run(tmp_path, make_monorepo(tmp_path), "import re\n" + source)
        assert _statuses(results) == ["pass"]


class TestUnanchoredLabelledNumbers:
    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('m = re.search(r"Ratio:\\s*(\\d+(?:\\.\\d+)?)", output)\n', id="decimal"),
            pytest.param(
                'm = re.search(r"VERDICT:\\s*([\\d.]+)", text, re.IGNORECASE)\n', id="digit_class"
            ),
            pytest.param('m = re.search(r"Score: (\\d+)", text)\n', id="literal_space"),
            pytest.param('m = re.findall(r"score=(\\d+)", text)\n', id="equals_separator"),
        ],
    )
    def test_a_labelled_number_searched_anywhere_warns(self, tmp_path, source):
        results = _run(tmp_path, make_monorepo(tmp_path), "import re\n" + source)
        assert _statuses(results) == ["warn"]
        (diagnostic,) = _diagnostics(results)
        assert diagnostic.severity == "warning"
        assert diagnostic.hint is not None
        assert "exit code" in diagnostic.hint

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('m = re.search(r"(?m)^Ratio:\\s*(\\d+)", output)\n', id="line_anchored"),
            pytest.param('m = re.search(r"Ratio:\\s*(\\d+)$", output)\n', id="end_anchored"),
            pytest.param('m = re.search(r"Action:\\s*([^\\n]+)", text)\n', id="free_text_capture"),
            pytest.param('m = re.search(r"Final Answer:\\s*(.+)", text)\n', id="any_capture"),
            pytest.param('m = re.search(r"(\\d+)", text)\n', id="number_without_a_label"),
            pytest.param(
                'm = re.search(r"\\bRatio:\\s*(\\d+)", output)\n', id="word_boundary_before_label"
            ),
        ],
    )
    def test_anchored_or_non_numeric_labelled_patterns_pass(self, tmp_path, source):
        results = _run(tmp_path, make_monorepo(tmp_path), "import re\n" + source)
        assert _statuses(results) == ["pass"]


class TestReporting:
    def test_findings_of_every_shape_are_listed_in_file_order(self, tmp_path):
        source = (
            "import re\n"
            "\n"
            "def parse(text, output):\n"
            '    ratio = re.search(r"Ratio:\\s*(\\d+)", output)\n'
            '    verdict = re.search(r"(yes|no)", text)\n'
            '    refused = "yes" in text\n'
            "    return ratio, verdict, refused\n"
        )
        results = _run(tmp_path, make_monorepo(tmp_path), source)
        assert [(d.line, d.severity) for d in _diagnostics(results)] == [
            (4, "warning"),
            (5, "error"),
            (6, "error"),
        ]

    def test_a_clean_package_passes(self, tmp_path):
        results = _run(
            tmp_path, make_monorepo(tmp_path), "def parse(text):\n    return text == 'yes'\n"
        )
        assert _statuses(results) == ["pass"]
        assert "No verdict" in results[0].message

    def test_test_files_are_not_read(self, tmp_path):
        config = make_monorepo(tmp_path)
        write(config.package_dir(tmp_path, "alpha") / "tests/test_judge.py", 'ok = "yes" in text\n')
        results = list(verdict_parse_anchored(LintContext.build(tmp_path, "alpha", config)))
        assert _statuses(results) == ["pass"]

    def test_a_standalone_repo_is_checked_too(self, tmp_path):
        results = _run(tmp_path, make_register_repo(tmp_path), 'ok = "yes" in text\n')
        assert _statuses(results) == ["fail"]

    def test_unparsable_file_is_reported(self, tmp_path):
        results = _run(tmp_path, make_monorepo(tmp_path), "def broken(:\n")
        assert "fail" in _statuses(results)
