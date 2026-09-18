"""The rules. Importing this package registers every rule.

Execution order is by category and code (see :func:`inspect_evals_lint.registry.rules`),
not by import order, so this list can stay alphabetical.
"""

from inspect_evals_lint.rules import (
    best_practices,
    code_quality,
    dependencies,
    file_structure,
    sandbox,
    tests,
)

__all__ = [
    "best_practices",
    "code_quality",
    "dependencies",
    "file_structure",
    "sandbox",
    "tests",
]
