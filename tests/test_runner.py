"""End-to-end behaviour of lint_package on both layouts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint import (
    PRESETS,
    ConfigError,
    LintConfig,
    evaluation_names,
    helper_names,
    lint_package,
    rule_names,
)
from inspect_evals_lint.registry import CATEGORIES, category_of, rules
from tests.conftest import (
    make_eval,
    make_helper,
    make_monorepo,
    make_register_repo,
    make_template_repo,
    write,
)


def statuses(
    root: Path, config: LintConfig, name: str = "alpha", **kwargs: object
) -> dict[str, list[str]]:
    report = lint_package(root, name, config, **kwargs)  # type: ignore[arg-type]
    return dict(report.statuses())


def first(report, name: str):
    """The first finding a rule produced."""
    return next(r for r in report.items() if r.rule is not None and r.rule.name == name)


HELPER_RULES = {r.name for r in rules() if "helper" in r.scopes}


def test_all_check_names_are_registered() -> None:
    assert rule_names() == sorted(r.name for r in rules())
    assert len(rule_names()) == 24


def test_every_check_has_a_category() -> None:
    assert {r.category for r in rules()} == set(CATEGORIES)
    assert category_of("readme") == "file_structure"
    assert category_of("IEFS006") == "file_structure"
    assert category_of("invalid_check") is None


@pytest.mark.parametrize("layout", ["monorepo", "template", "register"])
def test_well_formed_eval_passes_every_check(tmp_path: Path, layout: str) -> None:
    builders = {
        "monorepo": make_monorepo,
        "template": make_template_repo,
        "register": make_register_repo,
    }
    config = builders[layout](tmp_path)
    report = lint_package(tmp_path, "alpha", config)
    failing = [(d.rule.name, d.message) for d in report.diagnostics if d.status == "fail"]
    assert failing == []
    assert report.passed()
    assert set(report.statuses()) == set(rule_names())


def test_results_follow_registry_order(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    names = [r.rule.name for r in lint_package(root, "alpha", config).items()]
    deduped = list(dict.fromkeys(names))
    assert deduped == [r.name for r in rules()]
    assert deduped.index("main_file") < deduped.index("init_exports")


def test_missing_eval_reports_only_location(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    report = lint_package(root, "nope", config)
    assert report.statuses() == {"package_location": ["fail"]}
    assert "src/inspect_evals/nope" in report.diagnostics[0].message


def test_every_check_has_a_scope() -> None:
    assert all(r.scopes <= {"eval", "helper"} and "eval" in r.scopes for r in rules())
    # Structure and registration are properties of an evaluation, not of shared code.
    for name in ("main_file", "init_exports", "readme", "registry", "eval_yaml", "e2e_test"):
        assert name not in HELPER_RULES, name
    # The checks that guard scoring behaviour apply wherever the code lives.
    for name in ("model_role_resolution", "private_api_imports", "unscored_reason"):
        assert name in HELPER_RULES, name


def test_helper_dir_is_linted_with_the_helper_scope(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    report = lint_package(root, "utils", config)
    assert report.kind == "helper"
    assert report.passed()
    assert set(report.statuses()) == HELPER_RULES


def test_ignored_dir_is_skipped(tmp_path: Path) -> None:
    config = make_template_repo(tmp_path)
    write(tmp_path / "src/examples/__init__.py", "")
    report = lint_package(tmp_path, "examples", config)
    assert report.skipped is not None
    assert "ignore-dirs" in report.skipped
    assert report.items() == []
    assert report.passed()


def test_directory_without_init_is_not_a_package(monorepo: tuple[Path, LintConfig]) -> None:
    """A documentation-only directory such as inspect_evals' gdm_capabilities/ is skipped, not failed."""
    root, config = monorepo
    write(root / "src/inspect_evals/moved_evals/README.md", "# Moved\n")
    report = lint_package(root, "moved_evals", config)
    assert report.statuses() == {"package_location": ["skip"]}
    assert "__init__.py" in report.outcomes[0].message
    assert report.passed()
    # Under --check the location check is bypassed, and the outcome is the same.
    report = lint_package(root, "moved_evals", config, check="readme")
    assert report.items() == []


def test_check_outside_helper_scope_is_reported_as_not_applicable(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    report = lint_package(root, "utils", config, check="readme")
    assert report.statuses() == {"readme": ["skip"]}
    assert "helper" in report.outcomes[0].message


def test_helper_model_role_fallback_fails(monorepo: tuple[Path, LintConfig]) -> None:
    """The motivating case: a shared grader helper that inherits the self-grading fallback."""
    root, config = monorepo
    make_helper(
        root,
        config,
        code=(
            "from inspect_ai.model import get_model\n"
            "from inspect_ai.scorer import scorer\n\n"
            "@scorer(metrics=[])\n"
            "def graded(model_role='grader'):\n"
            "    async def score(state, target):\n"
            "        return get_model(role=model_role)\n"
            "    return score\n"
        ),
    )
    assert statuses(root, config, "utils")["model_role_resolution"] == ["fail"]
    config = replace(
        config, allowlists={"model_role_resolution": frozenset({("utils", "<dynamic>")})}
    )
    assert statuses(root, config, "utils")["model_role_resolution"] == ["warn"]


def test_helper_module_level_import_must_be_a_core_dependency(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    make_helper(root, config, code="import definitely_not_installed_pkg\n")
    report = lint_package(root, "utils", config)
    result = first(report, "external_dependencies")
    assert result.status == "fail"
    assert "[project].dependencies" in result.message
    # Declaring it in an optional group is not enough: every evaluation imports the helper.
    write(
        root / "pyproject.toml",
        '[project]\nname = "inspect_evals"\ndependencies = ["inspect_ai"]\n\n'
        "[project.optional-dependencies]\nsome_eval = ['definitely_not_installed_pkg']\n\n"
        '[tool.inspect-evals-lint]\npreset = "monorepo"\n',
    )
    assert statuses(root, config, "utils")["external_dependencies"] == ["fail"]
    write(
        root / "pyproject.toml",
        '[project]\nname = "inspect_evals"\n'
        'dependencies = ["inspect_ai", "definitely-not-installed-pkg"]\n\n'
        '[tool.inspect-evals-lint]\npreset = "monorepo"\n',
    )
    assert statuses(root, config, "utils")["external_dependencies"] == ["pass"]


def test_helper_lazy_import_needs_only_an_optional_group(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    make_helper(
        root,
        config,
        code=(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import typed_only_pkg\n"
            "try:\n"
            "    import guarded_pkg\n"
            "except ImportError:\n"
            "    guarded_pkg = None\n\n"
            "def load():\n"
            "    import lazy_pkg\n"
            "    return lazy_pkg\n"
        ),
    )
    report = lint_package(root, "utils", config)
    findings = [d for d in report.diagnostics if d.rule.name == "external_dependencies"]
    assert [d.status for d in findings] == ["fail", "fail", "fail"]
    assert all("optional-dependenc" in d.message for d in findings)
    assert all("[project].dependencies" not in d.message for d in findings)
    assert sorted(d.file.name for d in findings) == ["helpers.py"] * 3
    assert {d.line for d in findings} == {3, 5, 10}
    for name in ("lazy_pkg", "guarded_pkg", "typed_only_pkg"):
        assert any(name in d.message for d in findings)
    write(
        root / "pyproject.toml",
        '[project]\nname = "inspect_evals"\ndependencies = ["inspect_ai"]\n\n'
        "[project.optional-dependencies]\n"
        "some_eval = ['lazy_pkg', 'guarded_pkg', 'typed_only_pkg']\n\n"
        '[tool.inspect-evals-lint]\npreset = "monorepo"\n',
    )
    assert statuses(root, config, "utils")["external_dependencies"] == ["pass"]


def test_helper_lazy_import_declared_by_an_isolated_caller_passes(
    monorepo: tuple[Path, LintConfig],
) -> None:
    """inspect_evals' utils.huggingface defers ``import transformers`` for bold and novelty_bench, which are isolated."""
    root, config = monorepo
    make_helper(
        root, config, code="def load():\n    import transformers\n    return transformers\n"
    )
    assert statuses(root, config, "utils")["external_dependencies"] == ["fail"]
    write(
        root / "packages/bold/pyproject.toml",
        "[project]\nname = 'bold'\ndependencies = ['transformers>=5.0.0']\n",
    )
    assert statuses(root, config, "utils")["external_dependencies"] == ["pass"]


def test_eval_dependency_rule_is_unchanged_by_import_position(
    monorepo: tuple[Path, LintConfig],
) -> None:
    """Evaluations keep the group-per-evaluation rule wherever the import sits."""
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "extra.py",
        "def load():\n    import definitely_not_installed_pkg\n",
    )
    assert statuses(root, config)["external_dependencies"] == ["fail"]


def test_helper_scorer_tests_may_live_anywhere_under_tests(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    make_helper(
        root,
        config,
        code="from inspect_ai.scorer import scorer\n\n@scorer(metrics=[])\ndef shared_scorer():\n    ...\n",
    )
    assert statuses(root, config, "utils")["custom_scorer_tests"] == ["fail"]
    write(
        root / "tests/shared/test_shared.py",
        "from inspect_evals.utils.helpers import shared_scorer\n",
    )
    assert statuses(root, config, "utils")["custom_scorer_tests"] == ["pass"]


def test_helper_without_a_tests_dir_skips_tests_init(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    assert statuses(root, config, "utils")["tests_init"] == ["skip"]
    write(root / "tests/utils/test_x.py", "")
    assert statuses(root, config, "utils")["tests_init"] == ["fail"]
    write(root / "tests/utils/__init__.py", "")
    assert statuses(root, config, "utils")["tests_init"] == ["pass"]


def test_nested_utils_test_dir_is_no_longer_exempt_from_init_check(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    write(root / "tests/alpha/utils/helpers.py", "")
    assert statuses(root, config)["tests_init"] == ["fail"]


def test_get_all_helper_names_lists_only_helper_packages(tmp_path: Path) -> None:
    config = make_monorepo(tmp_path)
    config = replace(config, helper_dirs=frozenset({"utils", "common", "docs_dir"}))
    write(tmp_path / "src/inspect_evals/common/__init__.py", "")
    write(tmp_path / "src/inspect_evals/docs_dir/README.md", "")
    assert helper_names(tmp_path, config) == ["common", "utils"]
    assert "common" not in evaluation_names(tmp_path, config)


def test_missing_readme_fails(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    (config.package_dir(root, "alpha") / "README.md").unlink()
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
    assert statuses(root, config, check="IEFS006") == result


def test_invalid_check_name(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    with pytest.raises(ValueError, match="Unknown check"):
        lint_package(root, "alpha", config, check="bogus")


def test_disabled_checks_do_not_run(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    config = replace(config, ignore=("registry", "IETS002"))
    result = statuses(root, config)
    assert "registry" not in result
    assert "tests_init" not in result


def test_select_prefix_runs_only_that_category(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    result = statuses(root, replace(config, select=("IEFS",)))
    assert set(result) == {r.name for r in rules() if r.category == "file_structure"}


def test_exclude_keeps_files_out_of_the_ast_rules(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(config.package_dir(root, "alpha") / "challenges" / "bad.py", "print 'python 2'\n")
    assert statuses(root, config)["private_api_imports"] == ["fail"]  # the parse failure
    config = replace(config, exclude=("src/inspect_evals/*/challenges/**",))
    assert statuses(root, config)["private_api_imports"] == ["pass"]


def test_unparsable_file_is_reported_and_the_rest_still_checked(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    eval_dir = config.package_dir(root, "alpha")
    write(eval_dir / "broken.py", "def (:\n")
    write(eval_dir / "private.py", "from inspect_ai.model._model import x\n")
    report = lint_package(root, "alpha", config, check="private_api_imports")
    assert sorted(d.file.name for d in report.diagnostics) == ["broken.py", "private.py"]


def test_per_file_ignores_suppress_by_glob(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(config.package_dir(root, "alpha") / "data" / "gen.py", "s = Sample(input='x')\n")
    assert statuses(root, config)["sample_ids"] == ["fail"]
    config = replace(config, per_file_ignores=(("src/inspect_evals/*/data/**", ("IEBP003",)),))
    assert statuses(root, config)["sample_ids"] == ["suppressed"]


def test_legacy_noautolint_is_a_configuration_error(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(config.package_dir(root, "alpha") / ".noautolint", "readme\n")
    with pytest.raises(ConfigError, match="noautolint"):
        lint_package(root, "alpha", config)


def test_custom_required_yaml_fields(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    config = replace(config, eval_yaml_required_fields=("title", "license"))
    report = lint_package(root, "alpha", config)
    yaml_result = first(report, "eval_yaml")
    assert yaml_result.status == "fail"
    assert "license" in yaml_result.message


def test_get_all_eval_names_skips_non_evals_and_private_dirs(tmp_path: Path) -> None:
    config = make_monorepo(tmp_path, eval_names=("alpha", "beta"))
    write(tmp_path / "src/inspect_evals/_private/__init__.py", "")
    write(tmp_path / "src/inspect_evals/no_init/README.md", "")
    config = replace(config, ignore_dirs=frozenset({"scratch"}))
    write(tmp_path / "src/inspect_evals/scratch/__init__.py", "")
    assert evaluation_names(tmp_path, config) == ["alpha", "beta"]


def test_get_all_eval_names_template_layout(tmp_path: Path) -> None:
    config = make_template_repo(tmp_path, eval_names=("alpha", "beta"))
    write(tmp_path / "src/examples/__init__.py", "")
    assert evaluation_names(tmp_path, config) == ["alpha", "beta"]


def test_config_loaded_from_pyproject_when_omitted(monorepo: tuple[Path, LintConfig]) -> None:
    root, _ = monorepo
    assert lint_package(root, "alpha").passed()


def test_isolated_package_satisfies_dependency_check(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "extra.py",
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
        config.package_dir(root, "alpha") / "compose.yaml",
        "services:\n  default:\n    image: example/untagged\n",
    )
    assert statuses(root, config)["sandbox_image_pinning"] == ["fail"]
    config = replace(
        config, allowlists={"sandbox_image_pinning": frozenset({("alpha", "example/untagged")})}
    )
    assert statuses(root, config)["sandbox_image_pinning"] == ["warn"]
    (warning,) = lint_package(root, "alpha", config).statuses()["sandbox_image_pinning"]
    assert warning == "warn"


def test_stale_allowlist_entry_warns_at_pyproject(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    config = replace(
        config, allowlists={"sandbox_image_pinning": frozenset({("alpha", "example/untagged")})}
    )
    report = lint_package(root, "alpha", config, check="sandbox_image_pinning")
    assert report.statuses()["sandbox_image_pinning"] == ["skip", "warn"]
    (stale,) = report.diagnostics
    assert stale.file.name == "pyproject.toml"
    assert "no longer needed" in stale.message
    assert "allowlists.sandbox_image_pinning" in (stale.hint or "")


def test_allowlist_is_scoped_to_the_package(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "scorer.py",
        'from inspect_ai.model import get_model\n\ngrader = get_model(role="grader")\n',
    )
    config = replace(config, allowlists={"model_role_resolution": frozenset({("beta", "grader")})})
    assert statuses(root, config)["model_role_resolution"] == ["fail"]


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
    report = lint_package(root, "alpha", config)
    by_name = {r.rule.name: r for r in report.items()}
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
    write(config.package_dir(root, "alpha") / "README.md", "# alpha\n\nTODO: write me\n")
    report = lint_package(root, "alpha", config)
    readme = first(report, "readme")
    assert readme.status == "warn"  # the eval-dir README wins and its TODO is reported
    (config.package_dir(root, "alpha") / "README.md").unlink()
    (root / "README.md").unlink()
    report = lint_package(root, "alpha", config)
    readme = first(report, "readme")
    assert readme.status == "fail"
    assert "alpha/" in readme.message


def test_optional_eval_yaml_is_still_validated_when_present(
    register_repo: tuple[Path, LintConfig],
) -> None:
    root, config = register_repo
    write(config.package_dir(root, "alpha") / "eval.yaml", "title: Alpha\n")
    report = lint_package(root, "alpha", config)
    result = first(report, "eval_yaml")
    assert result.status == "fail"
    assert "description" in result.message


def test_tasks_py_accepted_as_main_file(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    eval_dir = config.package_dir(root, "alpha")
    (eval_dir / "alpha.py").rename(eval_dir / "tasks.py")
    write(eval_dir / "__init__.py", "from .tasks import alpha\n\n__all__ = ['alpha']\n")
    report = lint_package(root, "alpha", config)
    by_name = {r.rule.name: r for r in report.items()}
    assert by_name["main_file"].status == "pass"
    assert "tasks.py" in by_name["main_file"].message
    assert by_name["init_exports"].status == "pass"


def test_tasks_py_exports_are_checked(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    eval_dir = config.package_dir(root, "alpha")
    (eval_dir / "alpha.py").rename(eval_dir / "tasks.py")
    write(eval_dir / "__init__.py", "")
    result = statuses(root, config)
    assert result["main_file"] == ["pass"]
    assert result["init_exports"] == ["fail"]


def test_named_main_file_preferred_over_tasks_py(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    eval_dir = config.package_dir(root, "alpha")
    write(eval_dir / "tasks.py", "from inspect_ai import task\n\n@task\ndef other():\n    ...\n")
    report = lint_package(root, "alpha", config)
    main = first(report, "main_file")
    assert main.status == "pass"
    assert main.message.startswith("alpha.py")


def test_empty_named_main_file_does_not_hide_tasks_py(
    template_repo: tuple[Path, LintConfig],
) -> None:
    root, config = template_repo
    eval_dir = config.package_dir(root, "alpha")
    (eval_dir / "alpha.py").rename(eval_dir / "tasks.py")
    write(eval_dir / "alpha.py", "CONSTANT = 1\n")
    write(eval_dir / "__init__.py", "from .tasks import alpha\n")
    result = statuses(root, config)
    assert result["main_file"] == ["pass"]
    assert result["init_exports"] == ["pass"]


def test_missing_main_file_names_both_candidates(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    (config.package_dir(root, "alpha") / "alpha.py").unlink()
    report = lint_package(root, "alpha", config)
    by_name = {r.rule.name: r for r in report.items()}
    assert by_name["main_file"].status == "fail"
    assert by_name["main_file"].message == "Missing main file: alpha.py or tasks.py"
    assert by_name["init_exports"].status == "skip"


def test_model_role_allowlist_from_config(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.package_dir(root, "alpha") / "scorer.py",
        'from inspect_ai.model import get_model\n\ngrader = get_model(role="grader")\n',
    )
    assert statuses(root, config)["model_role_resolution"] == ["fail"]
    config = replace(config, allowlists={"model_role_resolution": frozenset({("alpha", "grader")})})
    assert statuses(root, config)["model_role_resolution"] == ["warn"]
    report = lint_package(root, "alpha", config, check="model_role_resolution")
    (d,) = report.diagnostics
    assert d.message.startswith("Allowlisted:")
    assert "remove the allowlist entry" in (d.hint or "")


def test_per_file_ignores_cover_a_finding_about_the_directory_itself(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    (root / "tests/alpha/test_alpha.py").write_text("def test_x():\n    pass\n")
    assert statuses(root, config, check="e2e_test")["e2e_test"] == ["fail"]
    config = replace(config, per_file_ignores=(("tests/alpha/**", ("e2e_test",)),))
    assert statuses(root, config, check="e2e_test")["e2e_test"] == ["suppressed"]


def test_dockerfile_findings_take_comment_and_path_suppressions(
    monorepo: tuple[Path, LintConfig],
) -> None:
    """A warning in a Dockerfile is suppressed by the comment above its instruction or a per-file glob."""
    root, config = monorepo
    eval_dir = config.package_dir(root, "alpha")
    write(
        eval_dir / "Dockerfile",
        "FROM python:3.12\n# inspect-evals-lint: ignore[IEBP007]\nRUN pip install numpy\n",
    )
    write(eval_dir / "images/Dockerfile", "FROM python:3.12\n")
    assert statuses(root, config)["dockerfile_locking"] == ["warn", "suppressed", "warn"]
    ignoring = replace(
        config, per_file_ignores=(("src/inspect_evals/*/images/**", ("dockerfile_locking",)),)
    )
    assert statuses(root, ignoring)["dockerfile_locking"] == ["warn", "suppressed", "suppressed"]
    report = lint_package(root, "alpha", ignoring)
    assert report.passed()


def test_dockerfile_locking_reads_its_option_table(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    eval_dir = config.package_dir(root, "alpha")
    write(root / "uv.lock", "")
    write(
        eval_dir / "Dockerfile",
        "# BUILD_CONTEXT=.\nFROM python:3.12@sha256:" + "a" * 64 + "\n"
        "COPY pyproject.toml uv.lock ./\nRUN uv sync --locked\n",
    )
    (warning,) = lint_package(root, "alpha", config, check="IEBP007").diagnostics
    assert "host project" in warning.message
    allowing = replace(config, rule_options={"dockerfile_locking": {"host_lock_coupling": "allow"}})
    assert statuses(root, allowing, check="IEBP007")["dockerfile_locking"] == ["pass"]
    broken = replace(config, rule_options={"dockerfile_locking": {"host_lock_coupling": "x"}})
    with pytest.raises(ConfigError, match="host-lock-coupling"):
        lint_package(root, "alpha", broken, check="IEBP007")
