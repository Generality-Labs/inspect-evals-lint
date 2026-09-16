"""End-to-end behaviour of lint_evaluation on both layouts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint import (
    PRESETS,
    LintConfig,
    get_all_check_names,
    get_all_eval_names,
    lint_evaluation,
)
from inspect_evals_lint.runner import CHECKS
from tests.conftest import make_eval, make_monorepo, make_register_repo, make_template_repo, write


def statuses(
    root: Path, config: LintConfig, name: str = "alpha", **kwargs: object
) -> dict[str, list[str]]:
    report = lint_evaluation(root, name, config, **kwargs)  # type: ignore[arg-type]
    out: dict[str, list[str]] = {}
    for r in report.results:
        out.setdefault(r.name, []).append(r.status)
    return out


def test_all_check_names_are_registered() -> None:
    assert get_all_check_names() == sorted(["eval_location", *CHECKS])
    assert len(get_all_check_names()) == 20


@pytest.mark.parametrize("layout", ["monorepo", "template", "register"])
def test_well_formed_eval_passes_every_check(tmp_path: Path, layout: str) -> None:
    builders = {
        "monorepo": make_monorepo,
        "template": make_template_repo,
        "register": make_register_repo,
    }
    config = builders[layout](tmp_path)
    report = lint_evaluation(tmp_path, "alpha", config)
    failing = [(r.name, r.message) for r in report.results if r.status == "fail"]
    assert failing == []
    assert report.passed()
    ran = {r.name for r in report.results}
    assert ran == set(get_all_check_names())


def test_results_follow_registry_order(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    names = [r.name for r in lint_evaluation(root, "alpha", config).results]
    deduped = list(dict.fromkeys(names))
    assert deduped == ["eval_location", *CHECKS]


def test_missing_eval_reports_only_location(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    report = lint_evaluation(root, "nope", config)
    assert [(r.name, r.status) for r in report.results] == [("eval_location", "fail")]
    assert "src/inspect_evals/nope" in report.results[0].message


def test_non_eval_dir_is_skipped(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    report = lint_evaluation(root, "utils", config)
    assert [(r.name, r.status) for r in report.results] == [("non_eval_directory", "skip")]


def test_missing_readme_fails(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    (config.eval_dir(root, "alpha") / "README.md").unlink()
    assert statuses(root, config)["readme"] == ["fail"]


def test_unregistered_eval_fails_module_registry(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(root / "src/inspect_evals/_registry.py", "")
    result = statuses(root, config)
    assert result["registry"] == ["fail"]


def test_unregistered_eval_fails_entry_point_registry(
    template_repo: tuple[Path, LintConfig],
) -> None:
    root, config = template_repo
    write(root / "pyproject.toml", "[project]\nname = 'x'\n")
    assert statuses(root, config)["registry"] == ["fail"]


def test_entry_point_registry_accepts_module_value(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    write(
        root / "pyproject.toml",
        "[project]\nname = 'x'\n[project.entry-points.inspect_ai]\nanything = \"alpha.alpha\"\n",
    )
    assert statuses(root, config)["registry"] == ["pass"]


def test_registry_none_skips(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    config = replace(config, registry="none")
    assert statuses(root, config)["registry"] == ["skip"]


def test_missing_tests_dir_fails_all_test_checks(tmp_path: Path) -> None:
    config = make_monorepo(tmp_path, eval_names=())
    write(tmp_path / "src/inspect_evals/_registry.py", "from inspect_evals.beta import beta\n")
    make_eval(tmp_path, config, "beta", with_tests=False)
    result = statuses(tmp_path, config, "beta")
    for check in (
        "tests_exist",
        "e2e_test",
        "tests_init",
        "record_to_sample_test",
        "custom_solver_tests",
        "custom_scorer_tests",
        "custom_tool_tests",
    ):
        assert result[check] == ["fail"], check


def test_only_one_check_runs_with_filter(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    result = statuses(root, config, check="readme")
    assert set(result) == {"readme"}


def test_invalid_check_name(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    result = statuses(root, config, check="bogus")
    assert result == {"invalid_check": ["fail"]}


def test_disabled_checks_do_not_run(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    config = replace(config, disabled_checks=frozenset({"registry", "tests_init"}))
    result = statuses(root, config)
    assert "registry" not in result
    assert "tests_init" not in result


def test_custom_required_yaml_fields(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    config = replace(config, eval_yaml_required_fields=("title", "license"))
    report = lint_evaluation(root, "alpha", config)
    yaml_result = next(r for r in report.results if r.name == "eval_yaml")
    assert yaml_result.status == "fail"
    assert "license" in yaml_result.message


def test_get_all_eval_names_skips_non_evals_and_private_dirs(tmp_path: Path) -> None:
    config = make_monorepo(tmp_path, eval_names=("alpha", "beta"))
    write(tmp_path / "src/inspect_evals/_private/__init__.py", "")
    write(tmp_path / "src/inspect_evals/no_init/README.md", "")
    assert get_all_eval_names(tmp_path, config) == ["alpha", "beta"]


def test_get_all_eval_names_template_layout(tmp_path: Path) -> None:
    config = make_template_repo(tmp_path, eval_names=("alpha", "beta"))
    write(tmp_path / "src/examples/__init__.py", "")
    assert get_all_eval_names(tmp_path, config) == ["alpha", "beta"]


def test_config_loaded_from_pyproject_when_omitted(monorepo: tuple[Path, LintConfig]) -> None:
    root, _ = monorepo
    assert lint_evaluation(root, "alpha").passed()


def test_isolated_package_satisfies_dependency_check(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.eval_dir(root, "alpha") / "extra.py",
        "import definitely_not_installed_pkg\n",
    )
    assert statuses(root, config)["external_dependencies"] == ["fail"]
    write(
        root / "packages/alpha/pyproject.toml",
        "[project]\nname = 'alpha'\ndependencies = ['definitely_not_installed_pkg']\n",
    )
    assert statuses(root, config)["external_dependencies"] == ["pass"]


def test_sandbox_allowlist_from_config(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.eval_dir(root, "alpha") / "compose.yaml",
        "services:\n  default:\n    image: example/untagged\n",
    )
    assert statuses(root, config)["sandbox_image_pinning"] == ["fail"]
    config = replace(config, sandbox_image_allowlist=frozenset({("alpha", "example/untagged")}))
    assert statuses(root, config)["sandbox_image_pinning"] == ["warn"]


def test_register_layout_statuses(register_repo: tuple[Path, LintConfig]) -> None:
    """Flat tests, root README and no eval.yaml are accepted rather than merely tolerated."""
    root, config = register_repo
    result = statuses(root, config)
    assert result["tests_exist"] == ["pass"]
    assert result["tests_init"] == ["skip"]
    assert result["e2e_test"] == ["pass"]
    assert result["record_to_sample_test"] == ["pass"]
    assert result["readme"] == ["pass"]
    assert result["eval_yaml"] == ["skip"]


def test_register_layout_same_repo_fails_under_template_preset(
    register_repo: tuple[Path, LintConfig],
) -> None:
    root, _ = register_repo
    result = statuses(root, replace(PRESETS["template"]))
    assert result["tests_exist"] == ["fail"]
    assert result["readme"] == ["fail"]
    assert result["eval_yaml"] == ["fail"]


def test_flat_layout_prefers_per_eval_directory(register_repo: tuple[Path, LintConfig]) -> None:
    """A template-derived repo that kept tests/<eval>/ is linted as before, __init__.py included."""
    root, config = register_repo
    write(config.tests_dir(root) / "alpha" / "test_other.py", "def test_x():\n    pass\n")
    report = lint_evaluation(root, "alpha", config)
    by_name = {r.name: r for r in report.results}
    assert "tests/alpha" in by_name["tests_exist"].message
    assert by_name["tests_init"].status == "fail"  # tests/alpha has no __init__.py
    assert by_name["e2e_test"].status == "fail"  # the flat file is no longer in scope


def test_flat_layout_needs_test_files(register_repo: tuple[Path, LintConfig]) -> None:
    root, config = register_repo
    (config.tests_dir(root) / "test_alpha.py").unlink()
    write(config.tests_dir(root) / "conftest.py", "")
    result = statuses(root, config)
    assert result["tests_exist"] == ["fail"]
    assert result["e2e_test"] == ["fail"]


def test_root_readme_fallback_only_when_eval_dir_has_none(
    register_repo: tuple[Path, LintConfig],
) -> None:
    root, config = register_repo
    write(config.eval_dir(root, "alpha") / "README.md", "# alpha\n\nTODO: write me\n")
    report = lint_evaluation(root, "alpha", config)
    readme = next(r for r in report.results if r.name == "readme")
    assert readme.status == "warn"  # the eval-dir README wins and its TODO is reported
    (config.eval_dir(root, "alpha") / "README.md").unlink()
    (root / "README.md").unlink()
    report = lint_evaluation(root, "alpha", config)
    readme = next(r for r in report.results if r.name == "readme")
    assert readme.status == "fail"
    assert "alpha/" in readme.message


def test_optional_eval_yaml_is_still_validated_when_present(
    register_repo: tuple[Path, LintConfig],
) -> None:
    root, config = register_repo
    write(config.eval_dir(root, "alpha") / "eval.yaml", "title: Alpha\n")
    report = lint_evaluation(root, "alpha", config)
    result = next(r for r in report.results if r.name == "eval_yaml")
    assert result.status == "fail"
    assert "description" in result.message
