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
    get_all_helper_names,
    lint_evaluation,
)
from inspect_evals_lint.runner import (
    CHECK_CATEGORIES,
    CHECK_SCOPES,
    CHECKS,
    HELPER_CHECKS,
    category_of,
)
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
    report = lint_evaluation(root, name, config, **kwargs)  # type: ignore[arg-type]
    out: dict[str, list[str]] = {}
    for r in report.results:
        out.setdefault(r.name, []).append(r.status)
    return out


def test_all_check_names_are_registered() -> None:
    assert get_all_check_names() == sorted(["eval_location", *CHECKS])
    assert len(get_all_check_names()) == 22


def test_every_check_has_a_category() -> None:
    assert set(CHECK_CATEGORIES) == set(get_all_check_names())
    assert set(CHECK_CATEGORIES.values()) == {
        "file_structure",
        "code_quality",
        "tests",
        "best_practices",
    }
    assert category_of("readme") == "file_structure"
    assert category_of("invalid_check") is None


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


def test_every_check_has_a_scope() -> None:
    assert set(CHECK_SCOPES) == set(get_all_check_names())
    assert all(scope <= {"eval", "helper"} and "eval" in scope for scope in CHECK_SCOPES.values())
    assert {name for name, scope in CHECK_SCOPES.items() if "helper" in scope} == HELPER_CHECKS
    # Structure and registration are properties of an evaluation, not of shared code.
    for name in ("main_file", "init_exports", "readme", "registry", "eval_yaml", "e2e_test"):
        assert name not in HELPER_CHECKS, name
    # The checks that guard scoring behaviour apply wherever the code lives.
    for name in ("model_role_resolution", "private_api_imports", "unscored_reason"):
        assert name in HELPER_CHECKS, name


def test_helper_dir_is_linted_with_the_helper_scope(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    report = lint_evaluation(root, "utils", config)
    assert report.kind == "helper"
    assert report.passed()
    ran = {r.name for r in report.results}
    assert ran == {"eval_location", *HELPER_CHECKS}


def test_ignored_dir_is_skipped(tmp_path: Path) -> None:
    config = make_template_repo(tmp_path)
    write(tmp_path / "src/examples/__init__.py", "")
    report = lint_evaluation(tmp_path, "examples", config)
    assert [(r.name, r.status) for r in report.results] == [("ignored_directory", "skip")]


def test_directory_without_init_is_not_a_package(monorepo: tuple[Path, LintConfig]) -> None:
    """A documentation-only directory such as inspect_evals' gdm_capabilities/ is skipped, not failed."""
    root, config = monorepo
    write(root / "src/inspect_evals/moved_evals/README.md", "# Moved\n")
    report = lint_evaluation(root, "moved_evals", config)
    assert [(r.name, r.status) for r in report.results] == [("eval_location", "skip")]
    assert "__init__.py" in report.results[0].message
    assert report.passed()
    # Under --check the location check is bypassed, and the outcome is the same.
    report = lint_evaluation(root, "moved_evals", config, check="readme")
    assert report.results == []


def test_check_outside_helper_scope_is_reported_as_not_applicable(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    report = lint_evaluation(root, "utils", config, check="readme")
    assert [(r.name, r.status) for r in report.results] == [("readme", "skip")]
    assert "helper" in report.results[0].message


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
    config = replace(config, model_role_allowlist=frozenset({("utils", "<dynamic>")}))
    assert statuses(root, config, "utils")["model_role_resolution"] == ["warn"]


def test_helper_module_level_import_must_be_a_core_dependency(
    monorepo: tuple[Path, LintConfig],
) -> None:
    root, config = monorepo
    make_helper(root, config, code="import definitely_not_installed_pkg\n")
    report = lint_evaluation(root, "utils", config)
    result = next(r for r in report.results if r.name == "external_dependencies")
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
    report = lint_evaluation(root, "utils", config)
    result = next(r for r in report.results if r.name == "external_dependencies")
    assert result.status == "fail"
    assert "optional-dependenc" in result.message
    assert "[project].dependencies" not in result.message
    for name in ("lazy_pkg", "guarded_pkg", "typed_only_pkg"):
        assert name in result.message
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
        config.eval_dir(root, "alpha") / "extra.py",
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
    assert get_all_helper_names(tmp_path, config) == ["common", "utils"]
    assert "common" not in get_all_eval_names(tmp_path, config)


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
    config = replace(config, ignore_dirs=frozenset({"scratch"}))
    write(tmp_path / "src/inspect_evals/scratch/__init__.py", "")
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


def test_tasks_py_accepted_as_main_file(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    eval_dir = config.eval_dir(root, "alpha")
    (eval_dir / "alpha.py").rename(eval_dir / "tasks.py")
    write(eval_dir / "__init__.py", "from .tasks import alpha\n\n__all__ = ['alpha']\n")
    report = lint_evaluation(root, "alpha", config)
    by_name = {r.name: r for r in report.results}
    assert by_name["main_file"].status == "pass"
    assert "tasks.py" in by_name["main_file"].message
    assert by_name["init_exports"].status == "pass"


def test_tasks_py_exports_are_checked(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    eval_dir = config.eval_dir(root, "alpha")
    (eval_dir / "alpha.py").rename(eval_dir / "tasks.py")
    write(eval_dir / "__init__.py", "")
    result = statuses(root, config)
    assert result["main_file"] == ["pass"]
    assert result["init_exports"] == ["fail"]


def test_named_main_file_preferred_over_tasks_py(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    eval_dir = config.eval_dir(root, "alpha")
    write(eval_dir / "tasks.py", "from inspect_ai import task\n\n@task\ndef other():\n    ...\n")
    report = lint_evaluation(root, "alpha", config)
    main = next(r for r in report.results if r.name == "main_file")
    assert main.status == "pass"
    assert main.message.startswith("alpha.py")


def test_empty_named_main_file_does_not_hide_tasks_py(
    template_repo: tuple[Path, LintConfig],
) -> None:
    root, config = template_repo
    eval_dir = config.eval_dir(root, "alpha")
    (eval_dir / "alpha.py").rename(eval_dir / "tasks.py")
    write(eval_dir / "alpha.py", "CONSTANT = 1\n")
    write(eval_dir / "__init__.py", "from .tasks import alpha\n")
    result = statuses(root, config)
    assert result["main_file"] == ["pass"]
    assert result["init_exports"] == ["pass"]


def test_missing_main_file_names_both_candidates(template_repo: tuple[Path, LintConfig]) -> None:
    root, config = template_repo
    (config.eval_dir(root, "alpha") / "alpha.py").unlink()
    report = lint_evaluation(root, "alpha", config)
    by_name = {r.name: r for r in report.results}
    assert by_name["main_file"].status == "fail"
    assert by_name["main_file"].message == "Missing main file: alpha.py or tasks.py"
    assert by_name["init_exports"].status == "skip"


def test_model_role_allowlist_from_config(monorepo: tuple[Path, LintConfig]) -> None:
    root, config = monorepo
    write(
        config.eval_dir(root, "alpha") / "scorer.py",
        'from inspect_ai.model import get_model\n\ngrader = get_model(role="grader")\n',
    )
    assert statuses(root, config)["model_role_resolution"] == ["fail"]
    config = replace(config, model_role_allowlist=frozenset({("alpha", "grader")}))
    assert statuses(root, config)["model_role_resolution"] == ["warn"]
