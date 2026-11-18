"""Role lookup must observe the bindings supplied when a component runs."""

from pathlib import Path

import pytest

from inspect_evals_lint.diagnostics import Diagnostic
from inspect_evals_lint.rules.best_practices import get_model_location
from tests.conftest import context_for, write


def findings(tmp_path: Path, source: str) -> list[Diagnostic]:
    package = tmp_path / "alpha"
    write(package / "__init__.py", "")
    write(package / "scorer.py", source)
    return [f for f in get_model_location(context_for(package)) if isinstance(f, Diagnostic)]


@pytest.mark.parametrize("lookup", ["get_model(role='grader')", "model_roles()"])
@pytest.mark.parametrize("decorator", ["scorer(metrics=[])", "solver", "agent()"])
def test_factory_body_is_early(tmp_path: Path, lookup: str, decorator: str) -> None:
    result = findings(tmp_path, f"@{decorator}\ndef factory():\n    model = {lookup}\n")
    assert len(result) == 1
    assert result[0].line == 3
    assert result[0].severity == "warning"
    assert "construction" in result[0].message


@pytest.mark.parametrize("lookup", ["get_model(role='grader')", "models.model_roles()"])
@pytest.mark.parametrize("decorator", ["scorer(metrics=[])", "solver", "agent()"])
def test_returned_callback_is_runtime(tmp_path: Path, lookup: str, decorator: str) -> None:
    assert not findings(
        tmp_path,
        f"@{decorator}\ndef factory():\n"
        f"    async def run_sample(state, target):\n        return {lookup}\n"
        "    return run_sample\n",
    )


@pytest.mark.parametrize(
    "source",
    [
        "model = get_model()",
        "@task\ndef task():\n    return model_roles()",
        "def helper(model=get_model()):\n    pass",
        "@scorer\ndef factory():\n    async def score(state, grader=get_model()):\n        pass\n    return score",
        "@scorer\ndef factory():\n    @decorate(model_roles())\n    async def score(state):\n        pass\n    return score",
        "@scorer\ndef factory():\n    class State:\n        model = get_model()",
        "@scorer\ndef factory():\n    model = (lambda: get_model())()",
        "@scorer\ndef factory():\n    return lambda model=get_model(): model",
    ],
)
def test_construction_expressions_are_reported(tmp_path: Path, source: str) -> None:
    result = findings(tmp_path, source)
    assert len(result) == 1
    assert "construction" in result[0].message


@pytest.mark.parametrize("early_call", ["", "    helper()\n"])
def test_nested_helper_is_classified_by_its_callers(tmp_path: Path, early_call: str) -> None:
    result = findings(
        tmp_path,
        "@scorer\ndef factory():\n"
        "    def helper():\n        return get_model()\n"
        + early_call
        + "    async def score(state, target):\n        return helper()\n"
        "    return score\n",
    )
    assert len(result) == bool(early_call)
    if result:
        assert result[0].line == 4


@pytest.mark.parametrize("early_call", ["", "helper()\n", "@task\ndef task():\n    helper()\n"])
def test_module_helper_is_classified_by_its_callers(tmp_path: Path, early_call: str) -> None:
    result = findings(
        tmp_path,
        "def helper():\n    return get_model()\n" + early_call + "@scorer\ndef factory():\n"
        "    async def score(state, target):\n        return helper()\n"
        "    return score\n",
    )
    assert len(result) == bool(early_call)


def test_imported_helper_and_reexport_are_followed(tmp_path: Path) -> None:
    package = tmp_path / "alpha"
    write(package / "helpers.py", "def helper():\n    return model_roles()\n")
    write(package / "exports.py", "from .helpers import helper as resolve\n")
    assert not findings(
        tmp_path,
        "from .exports import resolve\n"
        "@scorer\ndef factory():\n"
        "    async def score(state, target):\n        return resolve()\n"
        "    return score\n",
    )
    result = findings(
        tmp_path,
        "from .exports import resolve\n@scorer\ndef factory():\n    return resolve()\n",
    )
    assert len(result) == 1
    assert result[0].file == package / "helpers.py"
    assert "construction" in result[0].message


def test_returned_module_function_and_recursive_helpers(tmp_path: Path) -> None:
    assert not findings(
        tmp_path,
        "def helper():\n    helper()\n    return get_model()\n"
        "async def score(state, target):\n    return helper()\n"
        "@scorer\ndef factory():\n    return score\n",
    )


def test_factory_helper_can_return_callback(tmp_path: Path) -> None:
    assert not findings(
        tmp_path,
        "def make_score():\n"
        "    async def score(state, target):\n        return get_model()\n"
        "    return score\n"
        "@scorer\ndef factory():\n    return make_score()\n",
    )


def test_runtime_definition_default_is_allowed(tmp_path: Path) -> None:
    assert not findings(
        tmp_path,
        "@scorer\ndef factory():\n"
        "    async def score(state, target):\n"
        "        def helper(model=get_model()):\n            return model\n"
        "        return helper()\n"
        "    return score\n",
    )


def test_returned_lambda_is_runtime(tmp_path: Path) -> None:
    assert not findings(tmp_path, "@solver\ndef factory():\n    return lambda: get_model()\n")


def test_local_function_named_get_model_is_not_inspect(tmp_path: Path) -> None:
    assert not findings(tmp_path, "def get_model():\n    return 'value'\nmodel = get_model()\n")


def test_unknown_helper_is_reported_without_claiming_when_it_runs(tmp_path: Path) -> None:
    result = findings(tmp_path, "def helper():\n    return get_model()\n")
    assert len(result) == 1
    assert "no statically identified" in result[0].message


def test_builtin_role_forms_are_allowed(tmp_path: Path) -> None:
    assert not findings(
        tmp_path,
        "@scorer\ndef factory():\n"
        "    async def score(state, target):\n"
        "        model = None if 'grader' in model_roles() else DEFAULT\n"
        "        return await model_graded_qa(model=model)(state, target)\n"
        "    return score\n"
        "@task\ndef task():\n"
        "    return Task(scorer=model_graded_fact(model_role=ModelRole('grader', required=True)))\n",
    )
