"""The package version has one source: [project] version in pyproject.toml."""

import tomllib
from pathlib import Path

import inspect_evals_lint


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert inspect_evals_lint.__version__ == pyproject["project"]["version"]
