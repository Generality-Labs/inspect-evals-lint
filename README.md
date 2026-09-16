# inspect-evals-lint

Static checks for [Inspect AI](https://inspect.aisi.org.uk/) evaluations: file structure, test coverage conventions, best practices and sandbox image pinning.

These checks began life as the `autolint` tool inside [inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals). They are packaged here so any repository of Inspect evaluations can run the same checks, including standalone repos built from the [inspect-evals-template](https://github.com/Generality-Labs/inspect-evals-template) and submitted to the inspect_evals register.

Nothing is imported or executed from the evaluation being checked. Every check is static analysis over Python source (via `ast`), `eval.yaml`, compose files and `pyproject.toml`.

## Install

```bash
uv add --dev inspect-evals-lint
# or
pip install inspect-evals-lint
```

## Usage

```bash
inspect-evals-lint <eval_name>            # one evaluation
inspect-evals-lint --all-evals            # every evaluation in the repo
inspect-evals-lint --all-evals --summary-only
inspect-evals-lint --check-summary        # per-check compliance across evals
inspect-evals-lint <eval_name> --check registry
inspect-evals-lint --all-evals --json > lint.json
inspect-evals-lint --list-checks
```

`--json` writes one document to stdout and sends progress to stderr, so the output can be piped straight into other tooling. It carries `passed`, run-wide `summary` counts and, per evaluation, every check's `status`, `message`, `file` (relative to the repository root when possible) and `line`.

The repository root is the nearest `pyproject.toml` carrying a `[tool.inspect-evals-lint]` table (falling back to the nearest `pyproject.toml`, then the current directory). Pass `--root` to override.

Exit codes: `0` all checks passed (warnings, skips and suppressions count as passing), `1` at least one check failed, `2` usage or configuration error.

## Configuration

Configuration lives in `pyproject.toml`. Pick a layout preset and override any field:

```toml
[tool.inspect-evals-lint]
preset = "template"   # or "monorepo"
```

| Key                         | `template` preset                                | `monorepo` preset                | Meaning                                                                                                  |
| --------------------------- | ------------------------------------------------ | -------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `source-root`               | `src`                                            | `src/inspect_evals`              | Directory with one sub-directory per evaluation                                                          |
| `tests-root`                | `tests`                                          | `tests`                          | Directory holding `<tests-root>/<eval>/`                                                                 |
| `import-prefix`             | `""`                                             | `inspect_evals`                  | Dotted prefix evaluations import under                                                                   |
| `registry`                  | `entry-points`                                   | `module`                         | `entry-points` reads `[project.entry-points.inspect_ai]`; `module` greps a registry module; `none` skips |
| `registry-module`           | unset                                            | `src/inspect_evals/_registry.py` | Required when `registry = "module"`                                                                      |
| `non-eval-dirs`             | `["utils", "examples"]`                          | `["utils"]`                      | Sub-directories of `source-root` that are not evaluations                                                |
| `eval-yaml-required-fields` | `title, description, group, contributors, tasks` | same                             | Keys every `eval.yaml` must define                                                                       |
| `isolated-packages-dir`     | unset                                            | `packages`                       | Per-eval `pyproject.toml` directory for isolated dependency sets                                         |
| `disabled-checks`           | `[]`                                             | `[]`                             | Checks that never run                                                                                    |
| `sandbox-image-allowlist`   | `{}`                                             | `{}`                             | `{ eval = ["image/ref"] }` pairs allowed to stay unpinned (warn, not fail)                               |

Without a `[tool.inspect-evals-lint]` table the `template` preset is used. `--preset` overrides the table for one run.

## Suppressing a check

- Line: `# noautolint: <check_name>` on the offending line (checks that report per-site results: `private_api_imports`, `get_model_location`).
- File: `# noautolint-file: <check_name>` within the first ten lines of a file.
- Directory: a `.noautolint` file in a sub-directory listing check names, one per line. Files under that directory are also excluded from AST-based checks.
- Evaluation: a `.noautolint` file in the evaluation directory listing check names.

## Checks

See [docs/CHECKS.md](docs/CHECKS.md) for the full list with the reasoning behind each check.

## Python API

```python
from pathlib import Path
from inspect_evals_lint import lint_evaluation, load_config, get_all_eval_names

root = Path(".")
config = load_config(root)
for name in get_all_eval_names(root, config):
    report = lint_evaluation(root, name, config)
    print(name, report.passed(), report.summary())
```

## Development

```bash
uv sync
uv run pre-commit install   # optional: run the lint stack on every commit
uv run pytest
uv run basedpyright src
```

Linting (ruff, [zizmor](https://docs.zizmor.sh/), mdformat) runs via [pre-commit](https://pre-commit.com); CI runs the same stack plus basedpyright and pytest via the shared [`python-ci`](https://github.com/Generality-Labs/python-project-template) reusable workflow.

## Releasing

See [RELEASING.md](RELEASING.md).
