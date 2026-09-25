"""Config loading, presets, validation and root discovery."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from inspect_evals_lint.config import (
    PRESETS,
    REGISTER_CONFIG,
    ConfigError,
    LintConfig,
    config_from_table,
    find_repo_root,
    infer_preset,
    load_config,
    read_tool_table,
)
from inspect_evals_lint.registry import get_rule
from tests.conftest import write


def test_default_is_multi_eval_preset() -> None:
    assert config_from_table({}) == PRESETS["multi-eval"]
    assert PRESETS["multi-eval"].registry == "entry-points"
    assert PRESETS["multi-eval"].source_root == "src"


def test_preset_names_describe_layouts() -> None:
    assert sorted(PRESETS) == ["monorepo", "multi-eval", "single-eval"]
    assert "template" not in PRESETS  # aliases are not listed
    assert "register" not in PRESETS


def test_deprecated_names_resolve_with_a_warning() -> None:
    with pytest.warns(DeprecationWarning, match='use preset = "multi-eval"'):
        assert config_from_table({"preset": "template"}) == PRESETS["multi-eval"]
    with pytest.warns(DeprecationWarning, match="eval-yaml-required = false"):
        cfg = config_from_table({"preset": "register"})
    assert cfg == replace(PRESETS["single-eval"], eval_yaml_required=False)
    assert cfg == REGISTER_CONFIG
    with pytest.warns(DeprecationWarning, match="preset 'register' is deprecated"):
        assert PRESETS["register"] == REGISTER_CONFIG
    with pytest.warns(DeprecationWarning, match="preset 'template' is deprecated"):
        assert PRESETS["template"] == PRESETS["multi-eval"]
    with pytest.raises(KeyError):
        PRESETS["nope"]


def test_table_overrides_win_over_an_alias_override() -> None:
    with pytest.warns(DeprecationWarning, match="preset 'register' is deprecated"):
        cfg = config_from_table({"preset": "register", "eval-yaml-required": True})
    assert cfg.eval_yaml_required is True
    assert cfg.tests_layout == "flat"


def test_unknown_preset_lists_only_current_names() -> None:
    with pytest.raises(ConfigError, match=re.escape("['monorepo', 'multi-eval', 'single-eval']")):
        config_from_table({"preset": "nope"})


def test_monorepo_preset_values() -> None:
    cfg = PRESETS["monorepo"]
    assert cfg.source_root == "src/inspect_evals"
    assert cfg.helper_dirs == frozenset({"utils"})
    assert cfg.ignore_dirs == frozenset()
    assert cfg.import_prefix == "inspect_evals"
    assert cfg.registry == "module"
    assert cfg.registry_module == "src/inspect_evals/_registry.py"
    assert cfg.isolated_packages_dir == "packages"
    assert cfg.module_name("gpqa") == "inspect_evals.gpqa"
    assert PRESETS["multi-eval"].module_name("gpqa") == "gpqa"


def test_multi_eval_preset_directory_kinds() -> None:
    cfg = PRESETS["multi-eval"]
    assert cfg.helper_dirs == frozenset({"utils"})
    assert cfg.ignore_dirs == frozenset({"examples"})


def test_single_eval_preset_values() -> None:
    cfg = PRESETS["single-eval"]
    assert cfg.tests_layout == "flat"
    assert cfg.readme_location == "repo-root"
    assert cfg.eval_yaml_required is True  # a layout does not know where the metadata lives
    # Everything else follows the multi-eval layout.
    assert cfg.source_root == "src"
    assert cfg.registry == "entry-points"
    assert PRESETS["multi-eval"].tests_layout == "per-eval"
    assert PRESETS["multi-eval"].readme_location == "eval-dir"
    assert PRESETS["multi-eval"].eval_yaml_required is True
    assert PRESETS["monorepo"].per_eval_dependency_group is True
    assert PRESETS["multi-eval"].per_eval_dependency_group is False
    assert PRESETS["single-eval"].per_eval_dependency_group is False
    assert REGISTER_CONFIG.eval_yaml_required is False


def test_layout_keys_are_overridable() -> None:
    cfg = config_from_table(
        {"tests-layout": "flat", "readme_location": "repo-root", "eval-yaml-required": False}
    )
    assert cfg.tests_layout == "flat"
    assert cfg.readme_location == "repo-root"
    assert cfg.eval_yaml_required is False
    cfg = config_from_table({"preset": "single-eval", "eval-yaml-required": True})
    assert cfg.eval_yaml_required is True


def test_kebab_and_snake_keys_override_preset() -> None:
    cfg = config_from_table(
        {
            "preset": "monorepo",
            "helper-dirs": ["utils", "common"],
            "ignore_dirs": ["docs_only"],
            "eval-yaml-required-fields": ["title"],
            "select": ["IEFS", "IECQ", "model_role_resolution"],
            "ignore": ["IETS002"],
            "exclude": ["src/inspect_evals/*/challenges/**"],
            "per-file-ignores": {"src/inspect_evals/*/data/**": ["IEBP", "score_constants"]},
            "allowlists": {
                "sandbox_image_pinning": {"cybench": ["a/b", "c/d:latest"]},
                "model_role_resolution": {"moru": ["grader"], "makemesay": ["judge", "<dynamic>"]},
            },
            "readme": {"todo_marker": "FIXME"},
        }
    )
    assert cfg.helper_dirs == frozenset({"utils", "common"})
    assert cfg.ignore_dirs == frozenset({"docs_only"})
    assert cfg.eval_yaml_required_fields == ("title",)
    assert cfg.select == ("IEFS", "IECQ", "model_role_resolution")
    assert cfg.ignore == ("IETS002",)
    assert cfg.exclude == ("src/inspect_evals/*/challenges/**",)
    assert cfg.per_file_ignores == (("src/inspect_evals/*/data/**", ("IEBP", "score_constants")),)
    assert cfg.allowlists["sandbox_image_pinning"] == frozenset(
        {("cybench", "a/b"), ("cybench", "c/d:latest")}
    )
    assert cfg.allowlists["model_role_resolution"] == frozenset(
        {("moru", "grader"), ("makemesay", "judge"), ("makemesay", "<dynamic>")}
    )
    assert cfg.rule_options == {
        "readme": {"todo_marker": "FIXME"},
        **PRESETS["monorepo"].rule_options,  # a user table adds to the preset's defaults
    }
    assert cfg.source_root == "src/inspect_evals"


def test_rule_option_keys_accept_kebab_case() -> None:
    cfg = config_from_table({"readme": {"todo-marker": "FIXME"}})
    assert cfg.rule_options == {"readme": {"todo_marker": "FIXME"}}


def test_rule_option_table_merges_over_the_preset_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting one option leaves the preset's other options, and other rules' tables, in place."""
    from dataclasses import replace

    preset = replace(
        PRESETS["multi-eval"],
        rule_options={"readme": {"todo_marker": "TODO", "max_lines": 10}, "sample_ids": {"x": 1}},
    )
    monkeypatch.setitem(PRESETS, "with-options", preset)
    cfg = config_from_table({"preset": "with-options", "readme": {"todo-marker": "FIXME"}})
    assert cfg.rule_options == {
        "readme": {"todo_marker": "FIXME", "max_lines": 10},
        "sample_ids": {"x": 1},
    }
    assert config_from_table({"preset": "with-options"}).rule_options == preset.rule_options


def test_selection_by_name_code_and_prefix() -> None:
    readme, sample_ids, tests_init = (
        get_rule("readme"),
        get_rule("sample_ids"),
        get_rule("tests_init"),
    )
    assert readme
    assert sample_ids
    assert tests_init
    everything = config_from_table({})
    assert all(everything.selects(r) for r in (readme, sample_ids, tests_init))
    cfg = config_from_table({"select": ["IEFS", "sample_ids"], "ignore": ["IEFS006"]})
    assert not cfg.selects(readme)  # ignored by code
    assert cfg.selects(sample_ids)  # selected by name
    assert not cfg.selects(tests_init)  # never selected
    assert config_from_table({"ignore": ["IETS"]}).selects(readme)
    assert not config_from_table({"ignore": ["IETS"]}).selects(tests_init)


def test_path_policies() -> None:
    readme, sample_ids = get_rule("readme"), get_rule("sample_ids")
    assert readme
    assert sample_ids
    cfg = config_from_table(
        {
            "exclude": ["src/*/challenges/**", "src/vendored.py"],
            "per-file-ignores": {"src/*/data/**": ["IEBP"], "tests/**": ["readme"]},
        }
    )
    assert cfg.excludes("src/cybench/challenges/x/solve.py")
    assert cfg.excludes("src/vendored.py")
    assert not cfg.excludes("src/cybench/cybench.py")
    assert cfg.ignored_in("src/alpha/data/gen.py", sample_ids)
    assert not cfg.ignored_in("src/alpha/data/gen.py", readme)
    assert cfg.ignored_in("tests/alpha/test_x.py", readme)
    assert not cfg.ignored_in("src/alpha/alpha.py", sample_ids)
    # A finding about a directory carries the directory as its file; `dir/**` covers it.
    assert cfg.ignored_in("tests", readme)
    assert cfg.excludes("src/cybench/challenges")


def test_allowlist_lookup_is_per_package() -> None:
    pinning = get_rule("sandbox_image_pinning")
    assert pinning
    cfg = config_from_table(
        {"allowlists": {"sandbox_image_pinning": {"cybench": ["a/b"], "other": ["c/d"]}}}
    )
    assert cfg.allowlist_for(pinning, "cybench") == frozenset({"a/b"})
    assert cfg.allowlist_for(pinning, "nope") == frozenset()
    assert config_from_table({}).allowlist_for(pinning, "cybench") == frozenset()


@pytest.mark.parametrize(
    "table",
    [
        {"preset": "nope"},
        {"bogus": 1},
        {"registry": "magic"},
        {"registry": "module"},
        {"helper-dirs": "utils"},
        {"ignore-dirs": "examples"},
        {"select": ["NOPE"]},
        {"ignore": ["IEXX001"]},
        {"per-file-ignores": {"x/**": ["ZZZ"]}},
        {"per-file-ignores": ["x/**"]},
        {"allowlists": {"readme": {"alpha": ["x"]}}},  # readme takes no allowlist
        {"allowlists": {"nope": {"alpha": ["x"]}}},
        {"allowlists": {"sandbox_image_pinning": {"alpha": "img"}}},
        {"readme": "not a table"},
        {"readme": {1: "x"}},
        {"tests-layout": "nested"},
        {"readme-location": "anywhere"},
        {"eval-yaml-required": "no"},
    ],
)
def test_invalid_tables_raise(table: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        config_from_table(table)


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("non-eval-dirs", "helper-dirs"),
        ("disabled-checks", "ignore"),
        ("sandbox-image-allowlist", "allowlists.sandbox_image_pinning"),
        ("model-role-allowlist", "allowlists.model_role_resolution"),
    ],
)
def test_removed_keys_name_their_replacement(key: str, replacement: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(replacement)):
        config_from_table({key: []})


def test_empty_string_clears_optional_paths() -> None:
    cfg = config_from_table({"preset": "monorepo", "isolated-packages-dir": "", "registry": "none"})
    assert cfg.isolated_packages_dir is None
    assert cfg.registry == "none"


def test_load_config_reads_pyproject(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'monorepo'\n")
    assert load_config(tmp_path) == PRESETS["monorepo"]
    assert load_config(tmp_path, preset="multi-eval") == PRESETS["multi-eval"]


def test_load_config_without_table_or_pyproject(tmp_path: Path) -> None:
    """With nothing under src/ there is no single evaluation to infer, so the template preset stands."""
    assert load_config(tmp_path) == PRESETS["multi-eval"]
    write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")
    assert load_config(tmp_path) == PRESETS["multi-eval"]


def test_single_evaluation_package_infers_the_register_preset(tmp_path: Path) -> None:
    write(tmp_path / "src/alpha/__init__.py", "")
    write(tmp_path / "src/utils/__init__.py", "")  # a helper does not count
    write(tmp_path / "src/examples/__init__.py", "")  # nor an ignored directory
    assert infer_preset(tmp_path, {}) == ("single-eval", "src/ holds one evaluation package")
    assert load_config(tmp_path) == PRESETS["single-eval"]
    write(tmp_path / "src/beta/__init__.py", "")
    assert infer_preset(tmp_path, {}) == ("multi-eval", "src/ holds 2 evaluation packages")
    assert load_config(tmp_path) == PRESETS["multi-eval"]


def test_inference_honours_the_table_layout_keys(tmp_path: Path) -> None:
    """A table without ``preset`` still says where the evaluations live and what is a helper."""
    write(tmp_path / "evals/alpha/__init__.py", "")
    write(tmp_path / "evals/shared/__init__.py", "")
    write(
        tmp_path / "pyproject.toml",
        "[tool.inspect-evals-lint]\nsource-root = 'evals'\nhelper-dirs = ['shared']\n",
    )
    table = read_tool_table(tmp_path)
    assert table is not None
    assert infer_preset(tmp_path, table) == ("single-eval", "evals/ holds one evaluation package")
    cfg = load_config(tmp_path)
    assert cfg.tests_layout == "flat"
    assert cfg.source_root == "evals"
    assert cfg.helper_dirs == frozenset({"shared"})


def test_named_preset_is_never_second_guessed(tmp_path: Path) -> None:
    write(tmp_path / "src/alpha/__init__.py", "")
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'multi-eval'\n")
    assert load_config(tmp_path) == PRESETS["multi-eval"]
    assert load_config(tmp_path, preset="monorepo") == PRESETS["monorepo"]


def test_load_config_bad_toml(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_find_repo_root_prefers_configured_pyproject(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'monorepo'\n")
    nested = tmp_path / "packages" / "x"
    write(nested / "pyproject.toml", "[project]\nname = 'x'\n")
    deep = nested / "src" / "y"
    deep.mkdir(parents=True)
    assert find_repo_root(deep) == tmp_path.resolve()


def test_find_repo_root_falls_back_to_nearest_pyproject(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert find_repo_root(deep) == tmp_path.resolve()


def test_find_repo_root_falls_back_to_start(tmp_path: Path) -> None:
    deep = tmp_path / "a"
    deep.mkdir()
    assert find_repo_root(deep) == deep.resolve()


def test_config_is_frozen() -> None:
    with pytest.raises(AttributeError):
        LintConfig().source_root = "x"  # type: ignore[misc]
