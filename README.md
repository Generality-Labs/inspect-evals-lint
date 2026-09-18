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

| Key                         | `template` preset                                | `monorepo` preset                | `register` preset | Meaning                                                                                                  |
| --------------------------- | ------------------------------------------------ | -------------------------------- | ----------------- | -------------------------------------------------------------------------------------------------------- |
| `source-root`               | `src`                                            | `src/inspect_evals`              | `src`             | Directory with one sub-directory per evaluation                                                          |
| `tests-root`                | `tests`                                          | `tests`                          | `tests`           | Directory holding `<tests-root>/<eval>/`                                                                 |
| `tests-layout`              | `per-eval`                                       | `per-eval`                       | `flat`            | `flat` also accepts test files directly under `tests-root` when `<tests-root>/<eval>/` is absent         |
| `readme-location`           | `eval-dir`                                       | `eval-dir`                       | `repo-root`       | `repo-root` also accepts the repository's top-level `README.md`                                          |
| `eval-yaml-required`        | `true`                                           | `true`                           | `false`           | Whether a missing `eval.yaml` fails (a present one is always validated)                                  |
| `import-prefix`             | `""`                                             | `inspect_evals`                  | `""`              | Dotted prefix evaluations import under                                                                   |
| `registry`                  | `entry-points`                                   | `module`                         | `entry-points`    | `entry-points` reads `[project.entry-points.inspect_ai]`; `module` greps a registry module; `none` skips |
| `registry-module`           | unset                                            | `src/inspect_evals/_registry.py` | unset             | Required when `registry = "module"`                                                                      |
| `helper-dirs`               | `["utils"]`                                      | `["utils"]`                      | same as template  | Shared-code packages under `source-root`, linted with the helper scope (see below)                       |
| `ignore-dirs`               | `["examples"]`                                   | `[]`                             | same as template  | Sub-directories of `source-root` that are never linted                                                   |
| `eval-yaml-required-fields` | `title, description, group, contributors, tasks` | same                             | same              | Keys every `eval.yaml` must define                                                                       |
| `isolated-packages-dir`     | unset                                            | `packages`                       | unset             | Per-eval `pyproject.toml` directory for isolated dependency sets                                         |
| `select`                    | `["IE"]`                                         | same                             | same              | Rules to run: names, codes or code prefixes (`IEFS`, `IECQ`, `IETS`, `IEBP`); the default selects all    |
| `ignore`                    | `[]`                                             | `[]`                             | `[]`              | Rules never to run, in the same forms; wins over `select`                                                |
| `exclude`                   | `[]`                                             | `[]`                             | `[]`              | Repository-relative globs of files the AST-based rules never read (code shipped into a sandbox)          |
| `per-file-ignores`          | `{}`                                             | `{}`                             | `{}`              | `{ "glob/**" = ["IEBP", "rule_name"] }`: findings in matching files are suppressed for those rules       |
| `allowlists.<rule>`         | `{}`                                             | `{}`                             | `{}`              | `{ package = ["key"] }` findings the rule reports as warnings; only rules that take an allowlist         |
| `<rule>`                    | `{}`                                             | `{}`                             | `{}`              | Options for one rule, passed through to it                                                               |

Without a `[tool.inspect-evals-lint]` table the `template` preset is used. `--preset` overrides the table for one run. A fuller table:

```toml
[tool.inspect-evals-lint]
preset = "monorepo"
ignore = ["IETS004"]                                   # by name, code or prefix
exclude = ["src/inspect_evals/*/challenges/**"]         # sandbox code: never parsed
per-file-ignores = { "src/inspect_evals/*/data/**" = ["IEBP003"] }

[tool.inspect-evals-lint.allowlists.model_role_resolution]
moru = ["grader"]                                       # package = [keys the rule names]

[tool.inspect-evals-lint.allowlists.sandbox_image_pinning]
cybench = ["example/untagged"]
```

An allowlisted finding is reported as a warning with its message prefixed `Allowlisted:`, and an entry that no finding matches is itself a warning at `pyproject.toml`, so the list can only shrink. Removed keys (`disabled-checks`, `non-eval-dirs`, `sandbox-image-allowlist`, `model-role-allowlist`) are rejected with a message naming their replacement.

Only Python packages are linted: a sub-directory of `source-root` without an `__init__.py` (a README left behind after a move, a data directory) is skipped by `--all-evals` and reported as a skip when named directly, so it needs no `ignore-dirs` entry. `ignore-dirs` is for packages you really do not want checked.

### Helper packages

Shared code that evaluations import, such as inspect_evals' `utils` package, is not an evaluation but does most of the same things: it grades, it resolves models, it imports third-party packages. Directories listed in `helper-dirs` are linted with the checks that guard that behaviour (`private_api_imports`, `score_constants`, `unscored_reason`, `get_model_location`, `model_role_resolution`, `sample_ids`, `task_overridable_defaults`, `sandbox_image_pinning`, `external_dependencies`, `tests_init` and the `custom_*_tests` checks) and not with the ones about an evaluation's structure and registration (`main_file`, `init_exports`, `readme`, `registry`, `eval_yaml`, `tests_exist`, `e2e_test`, `record_to_sample_test`). Two checks adapt: `external_dependencies` requires a helper's module-level third-party imports to be in `[project].dependencies`, since every evaluation that imports the helper loads them, while imports inside a function, a `try` block or an `if TYPE_CHECKING:` block only need declaring in some group or isolated package; and the `custom_*_tests` checks look for a helper's tests anywhere under `tests-root`, not only in `tests/<name>/`. Allowlists are keyed by package name, so `utils = ["grader"]` under `allowlists.model_role_resolution` works for a helper too. A failing helper check fails the run like any other.

The `register` preset is for an upstream repository listed in the [inspect_evals register](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/register/README.md): one evaluation, tests directly under `tests/`, the README at the repository root, and metadata held by the register entry rather than an `eval.yaml` in the repo. Run it from outside the repo with `inspect-evals-lint --root <clone> --preset register --all-evals --json`.

## Suppressing a finding

Every finding has a file and, where it is in a file's contents, a line, so one comment syntax covers every rule:

- Line: `# inspect-evals-lint: ignore[IEBP003]` on the offending line. Names, codes and code prefixes are accepted, comma-separated. A bare `ignore` or `ignore[]` is a configuration error, so a suppression always says what it silences.
- File: `# inspect-evals-lint: ignore-file[IEBP003]` within the first ten lines of the file.
- Paths: `per-file-ignores` in the configuration table, for whole directories.
- Not linted at all: `exclude`, for code that is shipped into a sandbox rather than run on the host.

The comment is namespaced with the tool's name rather than reusing ruff's `# noqa`: ruff reads every `# noqa` comment and warns about codes it does not know, so a shared spelling would turn each suppression into a ruff warning. The former `noautolint` comments and `.noautolint` files are rejected with a message naming the replacement. Suppressed findings still appear in reports, marked `[suppressed]`, and count as passing.

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
