"""Configuration: layout presets, ``[tool.inspect-evals-lint]`` loading, repo root discovery.

Every path assumption the rules make (where evals live, where their tests live,
how tasks are registered) is a field on :class:`LintConfig`. A preset supplies
defaults for a known layout; keys in the pyproject table override them. Rule
selection, suppression by path and allowlists are configuration too, so one
table describes everything about how a repository is linted.
"""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

if TYPE_CHECKING:
    from inspect_evals_lint.registry import Rule

TOOL_TABLE = "inspect-evals-lint"

RegistryMode = Literal["module", "entry-points", "none"]
REGISTRY_MODES: tuple[str, ...] = get_args(RegistryMode)

TestsLayout = Literal["per-eval", "flat"]
TESTS_LAYOUTS: tuple[str, ...] = get_args(TestsLayout)

ReadmeLocation = Literal["eval-dir", "repo-root"]
README_LOCATIONS: tuple[str, ...] = get_args(ReadmeLocation)

Allowlist = frozenset[tuple[str, str]]
"""``(package, key)`` pairs a rule tolerates as warnings; ``key`` is whatever the rule allowlists by (a role, an image)."""


class ConfigError(ValueError):
    """Raised when the ``[tool.inspect-evals-lint]`` table is invalid."""


def selector_matches(selector: str, rule: Rule) -> bool:
    """Whether a ``select`` / ``ignore`` entry names ``rule``: its name, its code, or a code prefix."""
    return selector == rule.name or rule.code.startswith(selector)


def _no_allowlists() -> dict[str, Allowlist]:
    return {}


def _no_options() -> dict[str, Mapping[str, object]]:
    return {}


@dataclass(frozen=True)
class LintConfig:
    """Layout and policy settings for one repository."""

    source_root: str = "src"
    """Directory (relative to the repo root) holding one sub-directory per evaluation."""

    tests_root: str = "tests"
    """Directory holding ``<tests_root>/<name>/`` test packages."""

    tests_layout: TestsLayout = "per-eval"
    """``per-eval`` requires ``<tests_root>/<name>/``; ``flat`` also accepts test files directly under ``tests_root``, as single-evaluation repositories usually have."""

    readme_location: ReadmeLocation = "eval-dir"
    """``eval-dir`` requires ``README.md`` inside the evaluation directory; ``repo-root`` also accepts the repository's top-level ``README.md``."""

    eval_yaml_required: bool = True
    """Whether a missing ``eval.yaml`` fails. False skips instead, for repositories whose metadata lives in the inspect_evals register; a present file is still validated."""

    import_prefix: str = ""
    """Dotted import prefix for evaluations, e.g. ``inspect_evals``; empty when an eval imports as ``<name>``."""

    registry: RegistryMode = "entry-points"
    """How tasks are registered: a Python module that imports every eval, ``[project.entry-points.inspect_ai]``, or not checked."""

    registry_module: str | None = None
    """Path of the registry module (relative to the repo root); required when ``registry == "module"``."""

    helper_dirs: frozenset[str] = frozenset({"utils"})
    """Sub-directories of ``source_root`` holding shared code rather than an evaluation.

    They are linted with the helper scope: the rules that guard code behaviour
    (private imports, score values, model roles, dependencies, tests for custom
    components) but not the ones about an evaluation's structure and registration.
    """

    ignore_dirs: frozenset[str] = frozenset({"examples"})
    """Sub-directories of ``source_root`` that are never linted."""

    eval_yaml_required_fields: tuple[str, ...] = (
        "title",
        "description",
        "group",
        "contributors",
        "tasks",
    )
    """Top-level keys every ``eval.yaml`` must define."""

    isolated_packages_dir: str | None = None
    """Directory of per-eval ``<dir>/<name>/pyproject.toml`` files that declare an eval's dependencies instead of a root extra."""

    select: tuple[str, ...] = ("IE",)
    """Rules to run: names, codes or code prefixes. The default prefix selects every rule."""

    ignore: tuple[str, ...] = ()
    """Rules never to run, in the same forms as ``select``. Wins over ``select``."""

    exclude: tuple[str, ...] = ()
    """Glob patterns, relative to the repository root, of files the AST-based rules never read.

    For code that is shipped into a sandbox rather than run on the host, such as
    challenge sources that are not even valid Python 3.
    """

    per_file_ignores: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """``(glob, selectors)`` pairs: findings in files matching the glob are suppressed for the selected rules."""

    allowlists: Mapping[str, Allowlist] = field(default_factory=_no_allowlists)
    """Per rule, the ``(package, key)`` pairs it reports as warnings instead of failures. Only rules declared with ``allowlist=True`` accept one."""

    rule_options: Mapping[str, Mapping[str, object]] = field(default_factory=_no_options)
    """``[tool.inspect-evals-lint.<rule>]`` tables, passed through to the rule that declares them."""

    def source_dir(self, root: Path) -> Path:
        return root / self.source_root

    def tests_dir(self, root: Path) -> Path:
        return root / self.tests_root

    def eval_dir(self, root: Path, name: str) -> Path:
        return self.source_dir(root) / name

    def module_name(self, name: str) -> str:
        """Import path of an evaluation package."""
        return f"{self.import_prefix}.{name}" if self.import_prefix else name

    def selects(self, rule: Rule) -> bool:
        """Whether ``rule`` runs under ``select`` / ``ignore``."""
        return any(selector_matches(s, rule) for s in self.select) and not any(
            selector_matches(s, rule) for s in self.ignore
        )

    def excludes(self, relative_path: str | PurePosixPath) -> bool:
        """Whether a repository-relative path matches an ``exclude`` glob."""
        text = str(relative_path)
        return any(fnmatch.fnmatchcase(text, pattern) for pattern in self.exclude)

    def ignored_in(self, relative_path: str | PurePosixPath, rule: Rule) -> bool:
        """Whether ``per-file-ignores`` suppresses ``rule`` for a repository-relative path."""
        text = str(relative_path)
        return any(
            fnmatch.fnmatchcase(text, pattern) and any(selector_matches(s, rule) for s in selectors)
            for pattern, selectors in self.per_file_ignores
        )

    def allowlist_for(self, rule: Rule, package: str) -> frozenset[str]:
        """The keys ``rule`` tolerates for ``package``."""
        return frozenset(
            k for (p, k) in self.allowlists.get(rule.name, frozenset()) if p == package
        )

    def options_for(self, rule: Rule) -> Mapping[str, object]:
        return self.rule_options.get(rule.name, {})


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
        ignore_dirs=frozenset(),
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

# Keys removed from the table, with the message that points at their replacement.
_REMOVED_KEYS: dict[str, str] = {
    "non_eval_dirs": (
        "'non-eval-dirs' was removed: list shared-code packages in 'helper-dirs' "
        "(linted with the helper scope) and directories to leave alone in 'ignore-dirs'. "
        "Directories without an __init__.py are never linted and need no entry."
    ),
    "disabled_checks": "'disabled-checks' was removed: list rule names, codes or code prefixes under 'ignore'.",
    "sandbox_image_allowlist": (
        "'sandbox-image-allowlist' was removed: move its entries to "
        "[tool.inspect-evals-lint.allowlists.sandbox_image_pinning]."
    ),
    "model_role_allowlist": (
        "'model-role-allowlist' was removed: move its entries to "
        "[tool.inspect-evals-lint.allowlists.model_role_resolution]."
    ),
}


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


def _known_selectors(values: list[str], key: str) -> tuple[str, ...]:
    from inspect_evals_lint.registry import rules  # lazy: the registry imports this module's types

    for selector in values:
        if not any(selector_matches(selector, r) for r in rules()):
            raise ConfigError(
                f"'{key}' entry {selector!r} names no rule; use a rule name, a code such as "
                "IEFS001, or a code prefix such as IEFS"
            )
    return tuple(values)


def _allowlists(value: object, key: str) -> dict[str, Allowlist]:
    from inspect_evals_lint.registry import get_rule  # lazy, see _known_selectors

    table: dict[object, object] = _expect(value, dict, key)
    out: dict[str, Allowlist] = {}
    for rule_name, packages in table.items():
        name = _expect(rule_name, str, key)
        rule = get_rule(name)
        if rule is None:
            raise ConfigError(f"'{key}.{name}': no rule is named {name!r}")
        if not rule.allowlist:
            raise ConfigError(f"'{key}.{name}': rule {name!r} does not take an allowlist")
        entries: dict[object, object] = _expect(packages, dict, f"{key}.{name}")
        pairs: set[tuple[str, str]] = set()
        for package, keys in entries.items():
            package_name = _expect(package, str, f"{key}.{name}")
            pairs.update((package_name, k) for k in _str_list(keys, f"{key}.{name}.{package_name}"))
        out[rule.name] = frozenset(pairs)
    return out


def _per_file_ignores(value: object, key: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    table: dict[object, object] = _expect(value, dict, key)
    return tuple(
        (_expect(pattern, str, key), _known_selectors(_str_list(selectors, key), key))
        for pattern, selectors in table.items()
    )


def _coerce(key: str, value: object) -> object:
    if key in ("helper_dirs", "ignore_dirs"):
        return frozenset(_str_list(value, key))
    if key == "eval_yaml_required_fields":
        return tuple(_str_list(value, key))
    if key in ("select", "ignore"):
        return _known_selectors(_str_list(value, key), key)
    if key == "exclude":
        return tuple(_str_list(value, key))
    if key == "per_file_ignores":
        return _per_file_ignores(value, key)
    if key == "allowlists":
        return _allowlists(value, key)
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

    Keys may be written in kebab-case (``source-root``) or snake_case. A key that
    is a rule's name is that rule's option table.
    """
    from inspect_evals_lint.registry import get_rule  # lazy, see _known_selectors

    normalised = {key.replace("-", "_"): value for key, value in table.items()}
    preset_name = normalised.pop("preset", DEFAULT_PRESET)
    if preset_name not in PRESETS:
        raise ConfigError(f"Unknown preset {preset_name!r}; choose from {sorted(PRESETS)}")

    overrides: dict[str, object] = {}
    rule_options: dict[str, Mapping[str, object]] = {}
    for key, value in normalised.items():
        if key in _REMOVED_KEYS:
            raise ConfigError(_REMOVED_KEYS[key])
        if key in _FIELD_NAMES and key != "rule_options":
            overrides[key] = _coerce(key, value)
            continue
        rule = get_rule(key)
        if rule is not None:
            rule_options[rule.name] = dict(cast(dict[str, object], _expect(value, dict, key)))
            continue
        raise ConfigError(f"Unknown [tool.{TOOL_TABLE}] key {key!r}")
    if rule_options:
        overrides["rule_options"] = rule_options

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
