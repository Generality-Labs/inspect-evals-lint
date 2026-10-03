"""The rules. Importing this package registers every rule.

Every public module in this package (any name not starting with ``_``) is
imported here, and its ``@rule`` functions register themselves, so a new rule
module needs no entry anywhere else. Execution order is by category and code
(see :func:`inspect_evals_lint.registry.rules`), not by import order.
"""

import importlib
import pkgutil

for _module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    if not _module.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_module.name}")
