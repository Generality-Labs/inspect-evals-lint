"""Only warn when a role selection reaches a built-in grader's model argument."""

from pathlib import Path

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
        "model_graded_qa(\n    model=get_model(role='grader'),  # inspect-evals-lint: ignore[IEBP017] - protocol selects the first grader only\n)\n",
    )
    report = lint_package(root, "alpha", config, check="IEBP017")
    assert report.statuses()["grader_panel_preservation"] == ["suppressed"]


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
