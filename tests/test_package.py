"""The distribution advertises its inline types (PEP 561), so mypy in consuming projects uses them."""

from pathlib import Path

import inspect_evals_lint


def test_package_ships_py_typed() -> None:
    assert (Path(inspect_evals_lint.__file__).parent / "py.typed").is_file()
