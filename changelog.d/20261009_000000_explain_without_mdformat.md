### Fixed

- `--explain` no longer fails with `ModuleNotFoundError: No module named 'mdformat'` when the package is installed from a wheel. It ran the rule's page through mdformat, which is only a docs dependency. The page is now printed as written, which renders the same in the terminal; the `doc` field of `--output-format json` can differ from `docs/rules/<code>.md` in whitespace and table rules.
