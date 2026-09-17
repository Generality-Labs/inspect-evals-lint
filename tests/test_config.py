"""Config loading, presets, validation and root discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspect_evals_lint.config import (
    PRESETS,
    ConfigError,
    LintConfig,
    config_from_table,
    find_repo_root,
    load_config,
)
from tests.conftest import write


def test_default_is_template_preset() -> None:
    assert config_from_table({}) == PRESETS["template"]
    assert PRESETS["template"].registry == "entry-points"
    assert PRESETS["template"].source_root == "src"


def test_monorepo_preset_values() -> None:
    cfg = PRESETS["monorepo"]
    assert cfg.source_root == "src/inspect_evals"
    assert cfg.import_prefix == "inspect_evals"
    assert cfg.registry == "module"
    assert cfg.registry_module == "src/inspect_evals/_registry.py"
    assert cfg.isolated_packages_dir == "packages"
    assert cfg.module_name("gpqa") == "inspect_evals.gpqa"
    assert PRESETS["template"].module_name("gpqa") == "gpqa"


def test_register_preset_values() -> None:
    cfg = PRESETS["register"]
    assert cfg.tests_layout == "flat"
    assert cfg.readme_location == "repo-root"
    assert cfg.eval_yaml_required is False
    # Everything else follows the template layout.
    assert cfg.source_root == "src"
    assert cfg.registry == "entry-points"
    assert PRESETS["template"].tests_layout == "per-eval"
    assert PRESETS["template"].readme_location == "eval-dir"
    assert PRESETS["template"].eval_yaml_required is True


def test_layout_keys_are_overridable() -> None:
    cfg = config_from_table(
        {"tests-layout": "flat", "readme_location": "repo-root", "eval-yaml-required": False}
    )
    assert cfg.tests_layout == "flat"
    assert cfg.readme_location == "repo-root"
    assert cfg.eval_yaml_required is False
    cfg = config_from_table({"preset": "register", "eval-yaml-required": True})
    assert cfg.eval_yaml_required is True


def test_kebab_and_snake_keys_override_preset() -> None:
    cfg = config_from_table(
        {
            "preset": "monorepo",
            "non-eval-dirs": ["utils", "gdm_capabilities"],
            "disabled_checks": ["tests_init"],
            "eval-yaml-required-fields": ["title"],
            "sandbox-image-allowlist": {"cybench": ["a/b", "c/d:latest"]},
            "model-role-allowlist": {"moru": ["grader"], "makemesay": ["judge", "<dynamic>"]},
        }
    )
    assert cfg.model_role_allowlist == frozenset(
        {("moru", "grader"), ("makemesay", "judge"), ("makemesay", "<dynamic>")}
    )
    assert cfg.non_eval_dirs == frozenset({"utils", "gdm_capabilities"})
    assert cfg.disabled_checks == frozenset({"tests_init"})
    assert cfg.eval_yaml_required_fields == ("title",)
    assert cfg.sandbox_image_allowlist == frozenset({("cybench", "a/b"), ("cybench", "c/d:latest")})
    assert cfg.source_root == "src/inspect_evals"


@pytest.mark.parametrize(
    "table",
    [
        {"preset": "nope"},
        {"bogus": 1},
        {"registry": "magic"},
        {"registry": "module"},
        {"non-eval-dirs": "utils"},
        {"sandbox-image-allowlist": {"e": "img"}},
        {"model-role-allowlist": {"e": "grader"}},
        {"tests-layout": "nested"},
        {"readme-location": "anywhere"},
        {"eval-yaml-required": "no"},
    ],
)
def test_invalid_tables_raise(table: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        config_from_table(table)


def test_empty_string_clears_optional_paths() -> None:
    cfg = config_from_table({"preset": "monorepo", "isolated-packages-dir": "", "registry": "none"})
    assert cfg.isolated_packages_dir is None
    assert cfg.registry == "none"


def test_load_config_reads_pyproject(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.inspect-evals-lint]\npreset = 'monorepo'\n")
    assert load_config(tmp_path) == PRESETS["monorepo"]
    assert load_config(tmp_path, preset="template") == PRESETS["template"]


def test_load_config_without_table_or_pyproject(tmp_path: Path) -> None:
    assert load_config(tmp_path) == PRESETS["template"]
    write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")
    assert load_config(tmp_path) == PRESETS["template"]


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
