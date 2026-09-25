"""Fixture repositories in both supported layouts.

Each builder writes a small but complete evaluation that satisfies every check,
so tests can assert a clean pass and then break one thing at a time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inspect_evals_lint.config import PRESETS, REGISTER_CONFIG, LintConfig
from inspect_evals_lint.context import LintContext

EVAL_MAIN = """
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import match
from inspect_ai.solver import generate


def record_to_sample(record):
    return Sample(input=record["q"], target=record["a"], id=record["id"])


@task
def {name}(solver=None, scorer=None):
    return Task(dataset=[Sample(input="hi", target="hi", id="1")], solver=solver or generate(), scorer=scorer or match())
"""

EVAL_INIT = 'from .{name} import {name}\n\n__all__ = ["{name}"]\n'

EVAL_YAML = """title: {title}
description: A fixture evaluation.
group: Reasoning
contributors: [someone]
tasks:
  - name: {name}
"""

TEST_FILE = """
from inspect_ai import eval

from {module} import {name}


def test_e2e():
    logs = eval({name}(), model="mockllm/model")
    assert logs[0].status == "success"


def test_record_to_sample():
    from {module}.{name} import record_to_sample
    assert record_to_sample({{"q": "a", "a": "b", "id": "x"}}).id == "x"
"""


def context_for(package_dir: Path, config: LintConfig | None = None) -> LintContext:
    """A context for a bare package directory, for unit-testing one rule in isolation.

    The directory's parent stands in for the repository root; there is no test tree.
    """
    return LintContext(
        root=package_dir.parent,
        name=package_dir.name,
        path=package_dir,
        kind="eval",
        config=config or PRESETS["multi-eval"],
        test_path=None,
        test_search_path=None,
    )


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def make_eval(root: Path, config: LintConfig, name: str, *, with_tests: bool = True) -> Path:
    """Create a passing evaluation named ``name`` under ``root`` for ``config``'s layout."""
    eval_dir = config.package_dir(root, name)
    write(eval_dir / "__init__.py", EVAL_INIT.format(name=name))
    write(eval_dir / f"{name}.py", EVAL_MAIN.format(name=name))
    write(eval_dir / "eval.yaml", EVAL_YAML.format(name=name, title=name.title()))
    write(eval_dir / "README.md", f"# {name}\n")
    if with_tests:
        tests_dir = config.tests_dir(root) / name
        write(tests_dir / "__init__.py", "")
        write(
            tests_dir / f"test_{name}.py",
            TEST_FILE.format(module=config.module_name(name), name=name),
        )
    return eval_dir


def make_helper(root: Path, config: LintConfig, name: str = "utils", code: str = "") -> Path:
    """Create a helper package named ``name`` under ``config.source_root`` with ``code`` in ``helpers.py``."""
    helper_dir = config.source_dir(root) / name
    write(helper_dir / "__init__.py", "")
    if code:
        write(helper_dir / "helpers.py", code)
    return helper_dir


def make_monorepo(root: Path, eval_names: tuple[str, ...] = ("alpha",)) -> LintConfig:
    """A repo shaped like inspect_evals: src/inspect_evals/<eval>, _registry.py, [tool] table."""
    config = PRESETS["monorepo"]
    write(
        root / "pyproject.toml",
        '[project]\nname = "inspect_evals"\ndependencies = ["inspect_ai"]\n\n'
        "[project.optional-dependencies]\n\n"
        '[tool.inspect-evals-lint]\npreset = "monorepo"\n',
    )
    write(root / "src/inspect_evals/__init__.py", "")
    write(root / "src/inspect_evals/utils/__init__.py", "")
    registry_lines = [f"from inspect_evals.{name} import {name}" for name in eval_names]
    write(root / "src/inspect_evals/_registry.py", "\n".join(registry_lines) + "\n")
    for name in eval_names:
        make_eval(root, config, name)
    return config


def make_template_repo(root: Path, eval_names: tuple[str, ...] = ("alpha",)) -> LintConfig:
    """A repo shaped like inspect-evals-template: src/<eval>, entry points, no [tool] table."""
    config = PRESETS["multi-eval"]
    entry_points = "\n".join(f'{name} = "{name}"' for name in eval_names)
    write(
        root / "pyproject.toml",
        '[project]\nname = "my-evals"\ndependencies = ["inspect_ai"]\n\n'
        f"[project.entry-points.inspect_ai]\n{entry_points}\n",
    )
    write(root / "src/utils/__init__.py", "")
    for name in eval_names:
        make_eval(root, config, name)
    return config


def make_register_repo(root: Path, name: str = "alpha") -> LintConfig:
    """A single-eval upstream repo as listed in the inspect_evals register.

    Tests sit directly under ``tests/``, the README is at the repo root and there
    is no ``eval.yaml`` (the register entry carries the metadata), so the config
    is what the register lint service passes: ``single-eval`` with ``eval.yaml``
    optional.
    """
    config = REGISTER_CONFIG
    write(
        root / "pyproject.toml",
        '[project]\nname = "my-eval"\ndependencies = ["inspect-ai"]\n\n'
        f'[project.entry-points.inspect_ai]\n{name} = "{name}"\n',
    )
    eval_dir = config.package_dir(root, name)
    write(eval_dir / "__init__.py", EVAL_INIT.format(name=name))
    write(eval_dir / f"{name}.py", EVAL_MAIN.format(name=name))
    write(root / "README.md", f"# {name}\n")
    write(
        config.tests_dir(root) / f"test_{name}.py",
        TEST_FILE.format(module=config.module_name(name), name=name),
    )
    return config


@pytest.fixture
def register_repo(tmp_path: Path) -> tuple[Path, LintConfig]:
    return tmp_path, make_register_repo(tmp_path)


@pytest.fixture
def monorepo(tmp_path: Path) -> tuple[Path, LintConfig]:
    return tmp_path, make_monorepo(tmp_path)


@pytest.fixture
def template_repo(tmp_path: Path) -> tuple[Path, LintConfig]:
    return tmp_path, make_template_repo(tmp_path)
