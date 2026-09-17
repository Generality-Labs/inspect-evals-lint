"""Configuration: layout presets, ``[tool.inspect-evals-lint]`` loading, repo root discovery.

Every path assumption the checks make (where evals live, where their tests live,
how tasks are registered) is a field on :class:`LintConfig`. A preset supplies
defaults for a known layout; keys in the pyproject table override them.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal, cast, get_args

TOOL_TABLE = "inspect-evals-lint"

RegistryMode = Literal["module", "entry-points", "none"]
REGISTRY_MODES: tuple[str, ...] = get_args(RegistryMode)

TestsLayout = Literal["per-eval", "flat"]
TESTS_LAYOUTS: tuple[str, ...] = get_args(TestsLayout)

ReadmeLocation = Literal["eval-dir", "repo-root"]
README_LOCATIONS: tuple[str, ...] = get_args(ReadmeLocation)


class ConfigError(ValueError):
    """Raised when the ``[tool.inspect-evals-lint]`` table is invalid."""


@dataclass(frozen=True)
class LintConfig:
    """Layout and policy settings for one repository."""

    source_root: str = "src"
    """Directory (relative to the repo root) holding one sub-directory per evaluation."""

    tests_root: str = "tests"
    """Directory holding ``<tests_root>/<eval_name>/`` test packages."""

    tests_layout: TestsLayout = "per-eval"
    """``per-eval`` requires ``<tests_root>/<eval_name>/``; ``flat`` also accepts test files directly under ``tests_root``, as single-evaluation repositories usually have."""

    readme_location: ReadmeLocation = "eval-dir"
    """``eval-dir`` requires ``README.md`` inside the evaluation directory; ``repo-root`` also accepts the repository's top-level ``README.md``."""

    eval_yaml_required: bool = True
    """Whether a missing ``eval.yaml`` fails. False skips instead, for repositories whose metadata lives in the inspect_evals register; a present file is still validated."""

    import_prefix: str = ""
    """Dotted import prefix for evaluations, e.g. ``inspect_evals``; empty when an eval imports as ``<eval_name>``."""

    registry: RegistryMode = "entry-points"
    """How tasks are registered: a Python module that imports every eval, ``[project.entry-points.inspect_ai]``, or not checked."""

    registry_module: str | None = None
    """Path of the registry module (relative to the repo root); required when ``registry == "module"``."""

    non_eval_dirs: frozenset[str] = frozenset({"utils", "examples"})
    """Sub-directories of ``source_root`` that are not evaluations."""

    eval_yaml_required_fields: tuple[str, ...] = (
        "title",
        "description",
        "group",
        "contributors",
        "tasks",
    )
    """Top-level keys every ``eval.yaml`` must define."""

    isolated_packages_dir: str | None = None
    """Directory of per-eval ``<dir>/<eval_name>/pyproject.toml`` files that declare an eval's dependencies instead of a root extra."""

    disabled_checks: frozenset[str] = frozenset()
    """Checks that never run."""

    sandbox_image_allowlist: frozenset[tuple[str, str]] = frozenset()
    """``(eval_name, image)`` pairs allowed to stay unpinned; each produces a warning instead of a failure."""

    model_role_allowlist: frozenset[tuple[str, str]] = frozenset()
    """``(eval_name, role)`` pairs whose ``get_model(role=...)`` calls may lack a deliberate resolution for now; each produces a warning instead of a failure."""

    def source_dir(self, root: Path) -> Path:
        return root / self.source_root

    def tests_dir(self, root: Path) -> Path:
        return root / self.tests_root

    def eval_dir(self, root: Path, eval_name: str) -> Path:
        return self.source_dir(root) / eval_name

    def module_name(self, eval_name: str) -> str:
        """Import path of an evaluation package."""
        return f"{self.import_prefix}.{eval_name}" if self.import_prefix else eval_name


PRESETS: dict[str, LintConfig] = {
    # A standalone eval repo shaped like Generality-Labs/inspect-evals-template:
    # evals directly under src/, registered via entry points.
    "template": LintConfig(),
    # The UKGovernmentBEIS/inspect_evals monorepo: evals under a shared package,
    # registered by a hand-maintained module, some with isolated dependency sets.
    "monorepo": LintConfig(
        source_root="src/inspect_evals",
        import_prefix="inspect_evals",
        registry="module",
        registry_module="src/inspect_evals/_registry.py",
        non_eval_dirs=frozenset({"utils"}),
        isolated_packages_dir="packages",
    ),
    # An upstream repo listed in the inspect_evals register: one evaluation,
    # tests directly under tests/, README at the repo root, and eval.yaml held
    # by the register entry rather than the repo.
    "register": LintConfig(
        tests_layout="flat",
        readme_location="repo-root",
        eval_yaml_required=False,
    ),
}
DEFAULT_PRESET = "template"

_FIELD_NAMES = {f.name for f in fields(LintConfig)}


def _expect(value: object, kind: type, key: str) -> Any:
    if not isinstance(value, kind):
        raise ConfigError(f"'{key}' must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _choice(value: object, key: str, choices: tuple[str, ...]) -> str:
    text: str = _expect(value, str, key)
    if text not in choices:
        raise ConfigError(f"'{key}' must be one of {list(choices)}, got {text!r}")
    return text


def _str_list(value: object, key: str) -> list[str]:
    items: list[object] = _expect(value, list, key)
    return [_expect(item, str, key) for item in items]


def _coerce(key: str, value: object) -> object:
    if key in ("non_eval_dirs", "disabled_checks"):
        return frozenset(_str_list(value, key))
    if key == "eval_yaml_required_fields":
        return tuple(_str_list(value, key))
    if key in ("sandbox_image_allowlist", "model_role_allowlist"):
        table: dict[object, object] = _expect(value, dict, key)
        pairs: set[tuple[str, str]] = set()
        for eval_name, images in table.items():
            name = _expect(eval_name, str, key)
            pairs.update((name, image) for image in _str_list(images, key))
        return frozenset(pairs)
    if key == "registry":
        return _choice(value, key, REGISTRY_MODES)
    if key == "tests_layout":
        return _choice(value, key, TESTS_LAYOUTS)
    if key == "readme_location":
        return _choice(value, key, README_LOCATIONS)
    if key == "eval_yaml_required":
        return _expect(value, bool, key)
    if key in ("registry_module", "isolated_packages_dir"):
        text = _expect(value, str, key)
        return text or None
    return _expect(value, str, key)


def config_from_table(table: Mapping[str, Any]) -> LintConfig:
    """Build a :class:`LintConfig` from a ``[tool.inspect-evals-lint]`` table.

    Keys may be written in kebab-case (``source-root``) or snake_case.
    """
    normalised = {key.replace("-", "_"): value for key, value in table.items()}
    preset_name = normalised.pop("preset", DEFAULT_PRESET)
    if preset_name not in PRESETS:
        raise ConfigError(f"Unknown preset {preset_name!r}; choose from {sorted(PRESETS)}")

    overrides: dict[str, object] = {}
    for key, value in normalised.items():
        if key not in _FIELD_NAMES:
            raise ConfigError(f"Unknown [tool.{TOOL_TABLE}] key {key!r}")
        overrides[key] = _coerce(key, value)

    config = replace(PRESETS[preset_name], **overrides)
    if config.registry == "module" and not config.registry_module:
        raise ConfigError("registry = \"module\" requires 'registry-module' to be set")
    return config


def read_tool_table(root: Path) -> Mapping[str, Any] | None:
    """Return the ``[tool.inspect-evals-lint]`` table from ``root/pyproject.toml``, or None if absent."""
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Could not parse {pyproject}: {e}") from e
    tool: Any = data.get("tool")
    if not isinstance(tool, dict):
        return None
    table: Any = cast(dict[str, Any], tool).get(TOOL_TABLE)
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError(f"[tool.{TOOL_TABLE}] must be a table")
    return cast(dict[str, Any], table)


def load_config(root: Path, preset: str | None = None) -> LintConfig:
    """Load configuration for the repository at ``root``.

    ``preset`` overrides the ``preset`` key in the pyproject table (or the
    default when there is no table).
    """
    table = dict(read_tool_table(root) or {})
    if preset is not None:
        table["preset"] = preset
    return config_from_table(table)


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root by walking up from ``start`` (default: cwd).

    Prefers the nearest ``pyproject.toml`` that has a ``[tool.inspect-evals-lint]``
    table, then the nearest ``pyproject.toml`` of any kind, then ``start`` itself.
    """
    start = (start or Path.cwd()).resolve()
    nearest_pyproject: Path | None = None
    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").exists():
            nearest_pyproject = nearest_pyproject or directory
            try:
                if read_tool_table(directory) is not None:
                    return directory
            except ConfigError:
                continue
    return nearest_pyproject or start
