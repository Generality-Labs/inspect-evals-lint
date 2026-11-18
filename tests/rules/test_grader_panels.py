"""Only warn when a role selection reaches a built-in grader's model argument."""

from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest

from inspect_evals_lint import LintConfig, lint_package
from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.rules.grader_panels import grader_panel_preservation
from tests.conftest import context_for, write


def findings(tmp_path: Path, source: str) -> list[Diagnostic]:
    package = tmp_path / "alpha"
    write(package / "__init__.py", "")
    write(package / "scorer.py", source)
    return [f for f in grader_panel_preservation(context_for(package)) if isinstance(f, Diagnostic)]


@pytest.mark.parametrize("scorer", ["model_graded_qa", "model_graded_fact"])
@pytest.mark.parametrize(
    ("setup", "value"),
    [
        ("", "get_model(role='grader')"),
        ("grader = get_model(role='grader', default=DEFAULT)\n", "grader"),
        ("grader: Model = get_model(role='judge')\n", "grader"),
        ("grader = get_model(role=role)\nother = grader\n", "other"),
        ("", "get_model(role='grader') if condition else DEFAULT"),
        ("", "(grader := get_model(role='grader'))"),
        ("grader = get_model(role='grader')\n", "grader or DEFAULT"),
    ],
)
def test_role_selection_reaches_builtin(
    tmp_path: Path, scorer: str, setup: str, value: str
) -> None:
    result = findings(tmp_path, setup + f"{scorer}(model={value})")
    assert len(result) == 1
    assert result[0].severity == "warning"
    assert result[0].line == setup.count("\n") + 1


@pytest.mark.parametrize(
    "source",
    [
        "model_graded_qa()",
        "model_graded_fact(model_role='grader')",
        "model_graded_qa(model_role=ModelRole('grader', required=True))",
        "model_graded_qa(model='mockllm/grader')",
        "model_graded_qa(model=get_model())",
        "model_graded_qa(model=get_model('mockllm/grader'))",
        "model_graded_qa(model=get_model(role=None))",
        "model_graded_qa(model=model_roles()['grader'])",
        "model_graded_fact(model=model_roles().get('grader', DEFAULT))",
        "get_model(role='grader').generate('grade this')",
        "custom_scorer(model=get_model(role='grader'))",
        "model_graded_qa(template=get_model(role='grader'))",
        "grader = get_model(role='grader')\ngrader = DEFAULT\nmodel_graded_qa(model=grader)",
        "grader = get_model(role='grader')\ndel grader\nmodel_graded_qa(model=grader)",
        "grader = get_model(role='grader')\nfor grader in model_roles()['grader']:\n    model_graded_qa(model=grader)",
        "from elsewhere import get_model\nmodel_graded_qa(model=get_model(role='grader'))",
        "from elsewhere import model_graded_qa\nmodel_graded_qa(model=get_model(role='grader'))",
        "def get_model(role):\n    return DEFAULT\nmodel_graded_qa(model=get_model(role='grader'))",
    ],
)
def test_other_model_policies_are_allowed(tmp_path: Path, source: str) -> None:
    assert not findings(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        "from inspect_ai.model import get_model as resolve\nfrom inspect_ai.scorer import model_graded_qa as graded\ngraded(model=resolve(role='grader'))",
        "import inspect_ai.model as models\nimport inspect_ai.scorer as scoring\nscoring.model_graded_qa(model=models.get_model(role='grader'))",
        "import inspect_ai\ninspect_ai.scorer.model_graded_fact(model=inspect_ai.model.get_model(role='grader'))",
        "resolve = get_model\ngraded = model_graded_qa\ngraded(model=resolve(role='grader'))",
    ],
)
def test_api_aliases_are_followed(tmp_path: Path, source: str) -> None:
    assert len(findings(tmp_path, source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "if 'grader' not in model_roles():\n    model_graded_qa(model=get_model(role='grader', default=DEFAULT))",
        "if 'grader' in model_roles():\n    model_graded_qa()\nelse:\n    model_graded_qa(model=get_model(role='grader', default=DEFAULT))",
        "model_graded_qa(model=None if 'grader' in model_roles() else get_model(role='grader', default=DEFAULT))",
        "model = None if 'grader' in model_roles() else DEFAULT\nmodel_graded_qa(model=model)",
        "if not ('grader' in model_roles()):\n    grader = get_model(role='grader', default=DEFAULT)\nelse:\n    grader = None\nmodel_graded_qa(model=grader)",
        "def score():\n    if 'grader' in model_roles():\n        return model_graded_qa()\n    return model_graded_qa(model=get_model(role='grader', default=DEFAULT))",
    ],
)
def test_unbound_role_fallback_is_allowed(tmp_path: Path, source: str) -> None:
    assert not findings(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        "if 'other' not in model_roles():\n    model_graded_qa(model=get_model(role='grader'))",
        "if 'grader' in model_roles():\n    model_graded_qa(model=get_model(role='grader'))",
        "if 'grader' not in model_roles():\n    pass\nmodel_graded_qa(model=get_model(role='grader'))",
        "if condition:\n    grader = get_model(role='grader')\nelse:\n    grader = DEFAULT\nmodel_graded_qa(model=grader)",
        "if condition:\n    grader = DEFAULT\nelse:\n    grader = get_model(role='grader')\nmodel_graded_qa(model=grader)",
        "grader = get_model(role='grader')\nif condition:\n    grader = DEFAULT\nmodel_graded_qa(model=grader)",
        "grader = DEFAULT\nfor x in items:\n    grader = get_model(role='grader')\nmodel_graded_qa(model=grader)",
        "grader = DEFAULT\ntry:\n    grader = get_model(role='grader')\nexcept ValueError:\n    pass\nmodel_graded_qa(model=grader)",
    ],
)
def test_possible_selection_on_a_branch_is_reported(tmp_path: Path, source: str) -> None:
    assert len(findings(tmp_path, source)) == 1


@pytest.mark.parametrize("late_assignment", [False, True])
def test_closure_captures_role_selection(tmp_path: Path, late_assignment: bool) -> None:
    assignment = "    grader = get_model(role='grader')\n"
    source = "def factory():\n"
    if not late_assignment:
        source += assignment
    source += "    async def score(state, target):\n        return await model_graded_qa(model=grader)(state, target)\n"
    if late_assignment:
        source += assignment
    source += "    return score\n"
    assert len(findings(tmp_path, source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "grader = get_model(role='grader')\ndef score(grader):\n    return model_graded_qa(model=grader)",
        "grader = get_model(role='grader')\ndef score():\n    grader = DEFAULT\n    return model_graded_qa(model=grader)",
        "def factory():\n    grader = get_model(role='grader')\n    def score():\n        return model_graded_qa(model=grader)\n    grader = DEFAULT\n    return score",
        "def first():\n    grader = get_model(role='grader')\ndef second():\n    return model_graded_qa(model=grader)",
        "def score(get_model):\n    return model_graded_qa(model=get_model(role='grader'))",
        "def score(model_graded_qa):\n    return model_graded_qa(model=get_model(role='grader'))",
        "def score():\n    return DEFAULT\n    model_graded_qa(model=get_model(role='grader'))",
    ],
)
def test_shadowing_and_reassignment_do_not_report(tmp_path: Path, source: str) -> None:
    assert not findings(tmp_path, source)


def test_default_argument_captures_definition_time_selection(tmp_path: Path) -> None:
    assert (
        len(
            findings(
                tmp_path,
                "grader = get_model(role='grader')\ndef score(model=grader):\n    return model_graded_qa(model=model)\ngrader = DEFAULT",
            )
        )
        == 1
    )


def test_different_role_selections_report_each_call_once(tmp_path: Path) -> None:
    result = findings(
        tmp_path,
        "grader = get_model(role='one') if flag else get_model(role='two')\nmodel_graded_qa(model=grader)\nmodel_graded_fact(model=grader)",
    )
    assert [f.line for f in result] == [2, 3]


def test_source_is_not_executed(tmp_path: Path) -> None:
    result = findings(
        tmp_path, "import missing_provider\nmodel_graded_qa(model=get_model(role='grader'))"
    )
    assert len(result) == 1


def test_parse_failure_is_reported(tmp_path: Path) -> None:
    result = findings(tmp_path, "def broken(")
    assert len(result) == 1
    assert "parse" in result[0].message.lower()


def test_suppression_with_reason_applies_to_multiline_call(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "grader.py",
        "model_graded_qa(\n    model=get_model(role='grader'),  # inspect-evals-lint: ignore[IEBP017] -- protocol selects the first grader only\n)\n",
    )
    report = lint_package(root, "alpha", replace(config, select=("IEBP017", "IECQ005")))
    assert report.statuses()["grader_panel_preservation"] == ["suppressed"]
    assert report.statuses()["suppression_syntax"] == ["pass"]


def test_class_fields_are_not_method_closure_variables(tmp_path: Path) -> None:
    assert not findings(
        tmp_path,
        "grader = DEFAULT\nclass Judge:\n    grader = get_model(role='grader')\n    def score(self):\n        return model_graded_qa(model=grader)",
    )


def test_comprehension_target_does_not_rebind_enclosing_variable(tmp_path: Path) -> None:
    result = findings(
        tmp_path,
        "grader = get_model(role='grader')\ndef score():\n    [model_graded_qa(model=grader) for grader in model_roles()['grader']]\n    return model_graded_qa(model=grader)",
    )
    assert [f.line for f in result] == [4]


def test_role_absence_during_definition_does_not_apply_to_later_callback(tmp_path: Path) -> None:
    result = findings(
        tmp_path,
        "if 'grader' not in model_roles():\n    def score():\n        return model_graded_qa(model=get_model(role='grader'))",
    )
    assert len(result) == 1


def test_shared_helper_package_is_checked(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        root / "src/inspect_evals/utils/grading.py",
        "def helper():\n    return model_graded_qa(model=get_model(role='grader'))\n",
    )
    report = lint_package(root, "utils", config, check="IEBP017")
    assert report.statuses()["grader_panel_preservation"] == ["warn"]


@pytest.mark.parametrize(
    ("expected", "source"),
    [
        pytest.param(
            1, "f = lambda: model_graded_qa(model=get_model(role='grader'))", id="lambda missed"
        ),
        pytest.param(
            1,
            "from typing import cast\nmodel_graded_qa(model=cast(Model, get_model(role='grader')))",
            id="cast missed",
        ),
        pytest.param(
            1,
            "model_graded_qa(model=[get_model(role='grader')])",
            id="list missed (documented limit)",
        ),
        pytest.param(
            1,
            "grader, t = get_model(role='grader'), 1\nmodel_graded_qa(model=grader)",
            id="tuple unpack missed (documented limit)",
        ),
        pytest.param(
            0,
            "\n        grader = get_model(role='grader')\n        match x:\n            case _:\n                grader = DEFAULT\n                model_graded_qa(model=grader)\n    ",
            id="match reassignment false positive",
        ),
        pytest.param(
            1,
            "\n        match x:\n            case _:\n                grader = get_model(role='grader')\n                model_graded_qa(model=grader)\n    ",
            id="match selection missed",
        ),
        pytest.param(
            0,
            "\n        grader = get_model(role='grader')\n        try:\n            grader = DEFAULT\n        except Exception:\n            grader = OTHER\n        model_graded_qa(model=grader)\n    ",
            id="try reassignment false positive",
        ),
        pytest.param(
            0,
            "\n        def score():\n            grader = get_model(role='grader')\n            try:\n                return DEFAULT\n            except Exception:\n                return OTHER\n            model_graded_qa(model=grader)\n    ",
            id="unreachable after try false positive",
        ),
        pytest.param(
            1,
            "\n        try:\n            grader = get_model(role='grader')\n            risky()\n        except Exception:\n            model_graded_qa(model=grader)\n    ",
            id="handler selection missed",
        ),
        pytest.param(
            1,
            "\n        class Judge:\n            def score(self):\n                return model_graded_qa(model=grader)\n        grader = get_model(role='grader')\n    ",
            id="class method late assignment missed",
        ),
        pytest.param(
            1,
            "\n        def score():\n            return model_graded_qa(model=grader)\n        grader = get_model(role='grader')\n    ",
            id="function late assignment control",
        ),
        pytest.param(
            0,
            "\n        grader = get_model(role='grader')\n        class Judge:\n            def score(self):\n                return model_graded_qa(model=grader)\n        grader = DEFAULT\n    ",
            id="class method reassignment false positive",
        ),
        pytest.param(
            0,
            "\n        grader = get_model(role='grader')\n        def score():\n            return model_graded_qa(model=grader)\n        grader = DEFAULT\n    ",
            id="function reassignment control",
        ),
        pytest.param(
            1,
            "\n        if bad:\n            raise RuntimeError()\n        from inspect_ai.scorer import model_graded_qa as grade\n        def score():\n            return grade(model=get_model(role='grader'))\n    ",
            id="alias after raise missed in closure",
        ),
        pytest.param(
            1,
            "\n        if bad:\n            raise RuntimeError()\n        from inspect_ai.scorer import model_graded_qa as grade\n        grade(model=get_model(role='grader'))\n    ",
            id="alias after raise direct control",
        ),
        pytest.param(
            1,
            "\n        try:\n            from inspect_ai.scorer import model_graded_qa as grade\n        except ImportError:\n            raise\n        grade(model=get_model(role='grader'))\n    ",
            id="try import alias missed",
        ),
        pytest.param(
            1,
            "\n        grader = None\n        for s in xs:\n            if grader:\n                model_graded_qa(model=grader)\n            grader = get_model(role='grader')\n    ",
            id="loop next iteration missed",
        ),
        pytest.param(
            0,
            "\n        grader = get_model(role='grader')\n        def score():\n            match value:\n                case grader:\n                    model_graded_qa(model=grader)\n    ",
            id="match capture shadow false positive",
        ),
        pytest.param(
            0,
            "\n        grader = get_model(role='grader')\n        *grader, last = defaults\n        model_graded_qa(model=grader)\n    ",
            id="starred target rebinding false positive",
        ),
    ],
)
def test_review_regressions(tmp_path: Path, expected: int, source: str) -> None:
    assert len(findings(tmp_path, dedent(source))) == expected


@pytest.mark.parametrize(
    ("expected", "source"),
    [
        (1, "grade = lambda: model_graded_qa(model=grader)\ngrader = get_model(role='grader')"),
        (
            0,
            "grader = get_model(role='grader')\ngrade = lambda grader: model_graded_qa(model=grader)",
        ),
        (
            0,
            "grader = get_model(role='grader')\ngrade = lambda: model_graded_qa(model=grader)\ngrader = DEFAULT",
        ),
        (
            1,
            "grader = get_model(role='grader')\ngrade = lambda model=grader: model_graded_qa(model=model)\ngrader = DEFAULT",
        ),
        (
            1,
            "class Judge:\n    grade = lambda: model_graded_qa(model=grader)\ngrader = get_model(role='grader')",
        ),
        (
            0,
            "grader = DEFAULT\nclass Judge:\n    grader = get_model(role='grader')\n    grade = lambda: model_graded_qa(model=grader)",
        ),
        (1, "def score(grade=model_graded_qa):\n    return grade(model=get_model(role='grader'))"),
        (
            1,
            "from typing import cast as typed\nmodel_graded_qa(model=typed(Model, get_model(role='grader')))",
        ),
        (
            0,
            "from elsewhere import cast\nmodel_graded_qa(model=cast(Model, get_model(role='grader')))",
        ),
        (
            0,
            "from typing import cast\ndef score(cast):\n    return model_graded_qa(model=cast(Model, get_model(role='grader')))",
        ),
        (
            1,
            "from typing import cast\nmodel_graded_qa(model=cast(typ=Model, val=get_model(role='grader')))",
        ),
        (1, "model_graded_qa(model=[*unknown, get_model(role='grader')])"),
        (1, "models = [get_model(role='grader')]\nmodel_graded_qa(model=[*models])"),
        (0, "grader, other = get_model(role='grader'), DEFAULT\nmodel_graded_qa(model=other)"),
        (1, "grader, other = get_model(role='grader'), DEFAULT\nmodel_graded_qa(model=grader)"),
        (
            1,
            "first, *rest, last = DEFAULT, get_model(role='grader'), OTHER\nmodel_graded_qa(model=rest)",
        ),
        (
            0,
            "first, *rest, last = get_model(role='grader'), DEFAULT, OTHER\nmodel_graded_qa(model=rest)",
        ),
        (
            1,
            "(grader, other), last = (get_model(role='grader'), DEFAULT), OTHER\nmodel_graded_qa(model=grader)",
        ),
        (
            0,
            "(grader, other), last = (get_model(role='grader'), DEFAULT), OTHER\nmodel_graded_qa(model=other)",
        ),
    ],
)
def test_expression_and_capture_boundaries(tmp_path: Path, expected: int, source: str) -> None:
    assert len(findings(tmp_path, source)) == expected


@pytest.mark.parametrize(
    ("expected", "source"),
    [
        (
            0,
            "grader = get_model(role='grader')\ntry:\n    risky()\nexcept Exception:\n    pass\nfinally:\n    grader = DEFAULT\nmodel_graded_qa(model=grader)",
        ),
        (
            1,
            "grader = DEFAULT\ntry:\n    risky()\nexcept Exception:\n    pass\nfinally:\n    grader = get_model(role='grader')\nmodel_graded_qa(model=grader)",
        ),
        (
            0,
            "def factory():\n    grader = get_model(role='grader')\n    def score():\n        return model_graded_qa(model=grader)\n    try:\n        return score\n    finally:\n        grader = DEFAULT",
        ),
        (
            0,
            "def score():\n    grader = get_model(role='grader')\n    try:\n        risky()\n    finally:\n        return DEFAULT\n    model_graded_qa(model=grader)",
        ),
        (
            0,
            "grader = get_model(role='grader')\ntry:\n    risky()\nexcept Exception as grader:\n    model_graded_qa(model=grader)",
        ),
        (
            1,
            "def score():\n    try:\n        from inspect_ai.scorer import model_graded_qa as grade\n    except ImportError:\n        return None\n    return grade(model=get_model(role='grader'))",
        ),
        (
            1,
            "def factory():\n    if bad:\n        return None\n    from inspect_ai.scorer import model_graded_qa as grade\n    def score():\n        return grade(model=get_model(role='grader'))\n    return score",
        ),
        (
            1,
            "grader = None\nfor item in items:\n    if flag:\n        grader = get_model(role='grader')\n        continue\n    model_graded_qa(model=grader)",
        ),
        (
            0,
            "grader = get_model(role='grader')\nfor item in items:\n    break\n    model_graded_qa(model=grader)",
        ),
        (
            0,
            "grader = DEFAULT\nfor item in items:\n    grader = get_model(role='grader')\nelse:\n    grader = DEFAULT\nmodel_graded_qa(model=grader)",
        ),
        (
            1,
            "grader = DEFAULT\nfor item in items:\n    grader = get_model(role='grader')\n    break\nelse:\n    grader = DEFAULT\nmodel_graded_qa(model=grader)",
        ),
        (
            1,
            "grader = get_model(role='grader')\nfor item in items:\n    grader = [grader]\nmodel_graded_qa(model=grader)",
        ),
        (
            1,
            "grader = None\nwhile flag:\n    model_graded_qa(model=grader)\n    grader = get_model(role='grader')",
        ),
        (
            1,
            "grader = get_model(role='grader')\nmatch value:\n    case _ if flag:\n        grader = DEFAULT\nmodel_graded_qa(model=grader)",
        ),
        (
            0,
            "grader = get_model(role='grader')\nmatch value:\n    case 1:\n        grader = DEFAULT\n    case _:\n        grader = OTHER\nmodel_graded_qa(model=grader)",
        ),
    ],
)
def test_control_flow_boundaries(tmp_path: Path, expected: int, source: str) -> None:
    assert len(findings(tmp_path, source)) == expected


@pytest.mark.parametrize(
    "source",
    [
        "score = (lambda: model_graded_qa(model=get_model(role='grader'))) if flag else None",
        "scores = [lambda: model_graded_qa(model=get_model(role='grader')) for _ in items]",
        "(grader := get_model(role='grader')) if flag else None\nmodel_graded_qa(model=grader)",
    ],
)
def test_expression_branches_retain_deferred_calls(tmp_path: Path, source: str) -> None:
    assert len(findings(tmp_path, source)) == 1


def test_finally_sees_selection_when_the_try_body_raises_before_reassignment(
    tmp_path: Path,
) -> None:
    assert (
        len(
            findings(
                tmp_path,
                "grader = get_model(role='grader')\ntry:\n    risky()\n    grader = DEFAULT\nfinally:\n    model_graded_qa(model=grader)",
            )
        )
        == 1
    )


@pytest.mark.parametrize(("name", "expected"), [("grader", 1), ("other", 0)])
def test_branch_merge_preserves_tuple_positions(tmp_path: Path, name: str, expected: int) -> None:
    source = (
        "pair = (get_model(role='grader'), DEFAULT) if flag else (get_model(role='judge'), OTHER)\ngrader, other = pair\n"
        f"model_graded_qa(model={name})"
    )
    assert len(findings(tmp_path, source)) == expected
