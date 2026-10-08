"""custom_metric_tests (IETS008): every @metric is run through the epoch reducer by a test."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint import LintConfig, lint_package
from inspect_evals_lint.config import ConfigError, config_from_table
from inspect_evals_lint.rules.tests import metric_helper_module
from tests.conftest import make_eval, make_helper, make_monorepo, make_template_repo, write

METRICS = """
from inspect_ai.scorer import Metric, SampleScore, metric


@metric
def win_rate() -> Metric:
    def m(scores: list[SampleScore]) -> float:
        return 0.0

    return m


@metric(name="loss_rate_v2")
def loss_rate() -> Metric:
    def m(scores: list[SampleScore]) -> float:
        return 0.0

    return m
"""

HELPER_IMPORT = "from tests.utils.metric_epochs import assert_agreeing_epochs_change_nothing\n"


def _results(root: Path, config: LintConfig) -> list[tuple[str, str]]:
    report = lint_package(root, "alpha", config, check="custom_metric_tests")
    return [(o.status, o.message) for o in report.outcomes] + [
        (d.severity, d.message) for d in report.diagnostics
    ]


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, LintConfig]:
    config = make_template_repo(tmp_path)
    write(config.package_dir(tmp_path, "alpha") / "metrics.py", METRICS)
    return tmp_path, config


def test_skips_without_custom_metrics(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    assert _results(root, config) == [("skip", "No custom metrics found")]


def test_warns_for_each_metric_no_helper_test_names(repo: tuple[Path, LintConfig]) -> None:
    root, config = repo
    results = _results(root, config)
    assert [severity for severity, _ in results] == ["warning", "warning"]
    assert "@metric win_rate() is not named by any test that imports metric_epochs" in results[0][1]
    assert "(registered as 'loss_rate_v2')" in results[1][1]


def test_a_mention_without_the_helper_import_does_not_count(
    repo: tuple[Path, LintConfig],
) -> None:
    root, config = repo
    write(
        config.tests_dir(root) / "alpha/test_metrics.py",
        "from alpha.metrics import loss_rate, win_rate\n",
    )
    assert len(_results(root, config)) == 2


def test_a_helper_test_naming_each_metric_passes(repo: tuple[Path, LintConfig]) -> None:
    root, config = repo
    write(
        config.tests_dir(root) / "alpha/test_metric_epochs.py",
        HELPER_IMPORT + "from alpha.metrics import win_rate\n\nloss_rate_v2 = None\n",
    )
    assert _results(root, config) == [
        ("pass", "All 2 custom metric(s) are tested through metric_epochs")
    ]


def test_the_helper_module_name_is_configurable(repo: tuple[Path, LintConfig]) -> None:
    root, config = repo
    write(
        config.tests_dir(root) / "alpha/test_metric_epochs.py",
        "import my_helpers.epochs as epochs\nfrom alpha.metrics import loss_rate, win_rate\n",
    )
    assert len(_results(root, config)) == 2
    custom = replace(config, rule_options={"custom_metric_tests": {"helper_module": "epochs"}})
    assert [status for status, _ in _results(root, custom)] == ["pass"]


@pytest.mark.parametrize(
    ("import_line", "counts"),
    [
        ("from tests.utils.metric_epochs import run_metrics", True),
        ("import tests.utils.metric_epochs as m", True),
        ("from tests.utils import metric_epochs", True),
        ("from tests.utils import foo, metric_epochs", True),
        ("import os, tests.utils.metric_epochs", True),
        ("from tests.utils.metric_epochs_extra import x", False),
        ("import tests.utils.my_metric_epochs", False),
        ("# from tests.utils.metric_epochs import run_metrics", False),
        ('DOC = """\nfrom tests.utils.metric_epochs import run_metrics\n"""', False),
    ],
)
def test_import_forms(repo: tuple[Path, LintConfig], import_line: str, counts: bool) -> None:
    root, config = repo
    write(
        config.tests_dir(root) / "alpha/test_metric_epochs.py",
        import_line + "\nfrom alpha.metrics import loss_rate, win_rate\n",
    )
    statuses = [status for status, _ in _results(root, config)]
    assert statuses == (["pass"] if counts else ["warning", "warning"])


def test_a_longer_name_does_not_cover_a_shorter_one(repo: tuple[Path, LintConfig]) -> None:
    root, config = repo
    write(
        config.tests_dir(root) / "alpha/test_metric_epochs.py",
        HELPER_IMPORT + "category_win_rate = loss_rate_v2 = None\n",
    )
    results = _results(root, config)
    assert len(results) == 1
    assert "win_rate()" in results[0][1]


def test_a_helper_package_metric_is_found_anywhere_under_tests(tmp_path: Path) -> None:
    config = make_monorepo(tmp_path)
    make_helper(tmp_path, config, code=METRICS)
    report = lint_package(tmp_path, "utils", config, check="custom_metric_tests")
    assert len(report.diagnostics) == 2
    write(
        tmp_path / "tests/alpha/test_metric_epochs.py",
        HELPER_IMPORT + "from inspect_evals.utils.helpers import loss_rate, win_rate\n",
    )
    report = lint_package(tmp_path, "utils", config, check="custom_metric_tests")
    assert [o.status for o in report.outcomes] == ["pass"]


def test_without_a_tests_dir_metrics_warn_and_none_skip(tmp_path: Path) -> None:
    config = make_template_repo(tmp_path, eval_names=())
    make_eval(tmp_path, config, "beta", with_tests=False)
    report = lint_package(tmp_path, "beta", config, check="custom_metric_tests")
    assert [o.status for o in report.outcomes] == ["skip"]
    write(config.package_dir(tmp_path, "beta") / "metrics.py", METRICS)
    report = lint_package(tmp_path, "beta", config, check="custom_metric_tests")
    assert [d.severity for d in report.diagnostics] == ["warning", "warning"]


def test_the_option_loads_from_pyproject_in_kebab_case() -> None:
    config = config_from_table({"custom_metric_tests": {"helper-module": "tests.utils.epochs"}})
    assert metric_helper_module(config.rule_options["custom_metric_tests"]) == "epochs"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"helper": "x"}, "unknown option"),
        ({"helper_module": "not a module"}, "must be a module name"),
        ({"helper_module": "tests..epochs"}, "must be a module name"),
        ({"helper_module": 3}, "must be a module name"),
    ],
)
def test_bad_options_are_config_errors(options: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        metric_helper_module(options)
