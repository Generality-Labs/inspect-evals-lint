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
inspect-evals-lint <eval_name>            # one evaluation (or helper package, e.g. utils)
inspect-evals-lint --all-evals            # every evaluation and helper package in the repo
inspect-evals-lint --all-evals --summary-only
inspect-evals-lint --check-summary        # per-check compliance across evals
inspect-evals-lint <eval_name> --check registry
inspect-evals-lint --all-evals --json > lint.json
inspect-evals-lint --list-checks
```

`--json` writes one document to stdout and sends progress to stderr, so the output can be piped straight into other tooling. It carries `schema_version`, `passed`, run-wide `summary` counts and a `packages` list. Each package has its `kind` (`eval` or `helper`), `outcomes` (one per rule that passed or did not apply) and `diagnostics` (one per finding, with `rule`, `code`, `category` (`file_structure`, `code_quality`, `tests` or `best_practices`), `severity`, `status`, `message`, `file` relative to the repository root, `line`, `column` and a `hint`). Every finding points at a file, and at a line where the finding is in a file's contents, so `# noautolint: <rule>` on that line suppresses it whatever the rule. The set of categories is stable: a new rule always joins one of the four, because badge and dashboard tooling keys on them.

The repository root is the nearest `pyproject.toml` carrying a `[tool.inspect-evals-lint]` table (falling back to the nearest `pyproject.toml`, then the current directory). Pass `--root` to override.

Exit codes: `0` all checks passed (warnings, skips and suppressions count as passing), `1` at least one check failed, `2` usage or configuration error.

## Configuration

Configuration lives in `pyproject.toml`. Pick a layout preset and override any field:

```toml
[tool.inspect-evals-lint]
preset = "template"   # or "monorepo" / "register"
```

| Key                         | `template` preset                                | `monorepo` preset                | `register` preset | Meaning                                                                                                   |
| --------------------------- | ------------------------------------------------ | -------------------------------- | ----------------- | --------------------------------------------------------------------------------------------------------- |
| `source-root`               | `src`                                            | `src/inspect_evals`              | `src`             | Directory with one sub-directory per evaluation                                                           |
| `tests-root`                | `tests`                                          | `tests`                          | `tests`           | Directory holding `<tests-root>/<eval>/`                                                                  |
| `tests-layout`              | `per-eval`                                       | `per-eval`                       | `flat`            | `flat` also accepts test files directly under `tests-root` when `<tests-root>/<eval>/` is absent          |
| `readme-location`           | `eval-dir`                                       | `eval-dir`                       | `repo-root`       | `repo-root` also accepts the repository's top-level `README.md`                                           |
| `eval-yaml-required`        | `true`                                           | `true`                           | `false`           | Whether a missing `eval.yaml` fails (a present one is always validated)                                   |
| `import-prefix`             | `""`                                             | `inspect_evals`                  | `""`              | Dotted prefix evaluations import under                                                                    |
| `registry`                  | `entry-points`                                   | `module`                         | `entry-points`    | `entry-points` reads `[project.entry-points.inspect_ai]`; `module` greps a registry module; `none` skips  |
| `registry-module`           | unset                                            | `src/inspect_evals/_registry.py` | unset             | Required when `registry = "module"`                                                                       |
| `helper-dirs`               | `["utils"]`                                      | `["utils"]`                      | same as template  | Shared-code packages under `source-root`, linted with the helper scope (see below)                        |
| `ignore-dirs`               | `["examples"]`                                   | `[]`                             | same as template  | Sub-directories of `source-root` that are never linted                                                    |
| `eval-yaml-required-fields` | `title, description, group, contributors, tasks` | same                             | same              | Keys every `eval.yaml` must define                                                                        |
| `isolated-packages-dir`     | unset                                            | `packages`                       | unset             | Per-eval `pyproject.toml` directory for isolated dependency sets                                          |
| `disabled-checks`           | `[]`                                             | `[]`                             | `[]`              | Checks that never run                                                                                     |
| `sandbox-image-allowlist`   | `{}`                                             | `{}`                             | `{}`              | `{ eval = ["image/ref"] }` pairs allowed to stay unpinned (warn, not fail)                                |
| `model-role-allowlist`      | `{}`                                             | `{}`                             | `{}`              | `{ eval = ["role"] }` pairs whose `get_model(role=...)` may lack a deliberate resolution (warn, not fail) |

Without a `[tool.inspect-evals-lint]` table the `template` preset is used. `--preset` overrides the table for one run.

Only Python packages are linted: a sub-directory of `source-root` without an `__init__.py` (a README left behind after a move, a data directory) is skipped by `--all-evals` and reported as a skip when named directly, so it needs no `ignore-dirs` entry. `ignore-dirs` is for packages you really do not want checked.

### Helper packages

Shared code that evaluations import, such as inspect_evals' `utils` package, is not an evaluation but does most of the same things: it grades, it resolves models, it imports third-party packages. Directories listed in `helper-dirs` are linted with the checks that guard that behaviour (`private_api_imports`, `score_constants`, `unscored_reason`, `get_model_location`, `model_role_resolution`, `sample_ids`, `task_overridable_defaults`, `sandbox_image_pinning`, `external_dependencies`, `tests_init` and the `custom_*_tests` checks) and not with the ones about an evaluation's structure and registration (`main_file`, `init_exports`, `readme`, `registry`, `eval_yaml`, `tests_exist`, `e2e_test`, `record_to_sample_test`). Two checks adapt: `external_dependencies` requires a helper's module-level third-party imports to be in `[project].dependencies`, since every evaluation that imports the helper loads them, while imports inside a function, a `try` block or an `if TYPE_CHECKING:` block only need declaring in some group or isolated package; and the `custom_*_tests` checks look for a helper's tests anywhere under `tests-root`, not only in `tests/<name>/`. Allowlists are keyed by directory name, so `utils = ["grader"]` under `model-role-allowlist` works for a helper too. A failing helper check fails the run like any other.

The `register` preset is for an upstream repository listed in the [inspect_evals register](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/register/README.md): one evaluation, tests directly under `tests/`, the README at the repository root, and metadata held by the register entry rather than an `eval.yaml` in the repo. Run it from outside the repo with `inspect-evals-lint --root <clone> --preset register --all-evals --json`.

## Suppressing a check

- Line: `# noautolint: <check_name>` on the offending line (checks that report per-site results: `private_api_imports`, `get_model_location`).
- File: `# noautolint-file: <check_name>` within the first ten lines of a file.
- Directory: a `.noautolint` file in a sub-directory listing check names, one per line. Files under that directory are also excluded from AST-based checks.
- Evaluation: a `.noautolint` file in the evaluation directory listing check names.

## Checks

See [docs/CHECKS.md](docs/CHECKS.md) for the full list with the reasoning behind each check. Each check is a *rule* with a code (`IEFS001`, `IECQ001`, `IETS001`, `IEBP001` for the four categories) declared with the `@rule` decorator in `src/inspect_evals_lint/rules/`; `--check` accepts either the code or the name.

## Python API

```python
from pathlib import Path
from inspect_evals_lint import (
    get_all_eval_names,
    get_all_helper_names,
    lint_evaluation,
    load_config,
)

root = Path(".")
config = load_config(root)
for name in (*get_all_eval_names(root, config), *get_all_helper_names(root, config)):
    report = lint_evaluation(root, name, config)
    print(name, report.kind, report.passed(), report.summary())
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
