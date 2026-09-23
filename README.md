# inspect-evals-lint

Static checks for [Inspect AI](https://inspect.aisi.org.uk/) evaluations: file structure, test coverage conventions, best practices and sandbox image pinning. Documentation, including a page per rule, is at [inspect-evals-lint.generality.org](https://inspect-evals-lint.generality.org/).

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
inspect-evals-lint gpqa                     # one evaluation (or helper package, e.g. utils)
inspect-evals-lint gpqa utils               # several
inspect-evals-lint --all                    # every evaluation and helper package in the repo
inspect-evals-lint --all --select IEBP      # only the best-practice rules
inspect-evals-lint gpqa --ignore readme,IETS
inspect-evals-lint --all --output-format json > lint.json
inspect-evals-lint --all --output-format markdown   # a summary for a pull request comment
inspect-evals-lint --preset register --task src/castle/castle.py   # the package holding a task file
inspect-evals-lint --list-rules             # every rule with its code, category, scope and summary
inspect-evals-lint --explain IEBP002        # one rule's documentation
```

Run it inside the project's environment (`uv run inspect-evals-lint`): `external_dependencies` maps import names to distributions through the packages installed there. `--select` replaces the configured selection for one run and `--ignore` adds to it; both take rule names, codes or code prefixes, comma-separated. With more than one package the text output ends with an overall summary, a per-rule compliance table and the failures grouped by rule.

`--output-format json` writes one document to stdout and sends progress to stderr, so the output can be piped straight into other tooling ([docs/output.md](docs/output.md) has the schema). `--output-format github` writes one GitHub Actions annotation per finding instead, each message starting with the file, line and rule, so a lint step marks up the pull request and the job log still names every location; in an Actions job it also appends the Markdown summary to the job summary (`GITHUB_STEP_SUMMARY`). `--output-format markdown` writes, per package, a headline of rules met with a per-category split and a collapsible list of the rules not met, with warnings or suppressed, for a pull request comment or job summary.

`--task <path>` (repeatable) lints the package holding a task file instead of a named package: the evaluation is the directory holding the file, its parent is the source root, and enclosing packages form the import prefix, so a repository can be linted from a register entry's `task_path` without knowing its layout. A task file with no `__init__.py` beside it is a usage error naming the problem. Everything but the layout comes from the configuration or `--preset`. It carries `schema_version`, `passed`, run-wide `summary` counts and a `packages` list. Each package has its `kind` (`eval` or `helper`), `outcomes` (one per rule that passed or did not apply) and `diagnostics` (one per finding, with `rule`, `code`, `category` (`file_structure`, `code_quality`, `tests` or `best_practices`), `severity`, `status`, `message`, `file` relative to the repository root, `line`, `column` and a `hint`). Every finding points at a file, and at a line where the finding is in a file's contents, so `# inspect-evals-lint: ignore[<rule>]` on that line suppresses it whatever the rule. The set of categories is stable: a new rule always joins one of the four, because badge and dashboard tooling keys on them.

The repository root is the nearest `pyproject.toml` carrying a `[tool.inspect-evals-lint]` table (falling back to the nearest `pyproject.toml`, then the current directory). Pass `--root` to override.

Exit codes: `0` all checks passed (warnings, skips and suppressions count as passing), `1` at least one check failed, `2` usage or configuration error.

## Configuration

Configuration lives in `pyproject.toml`. Pick a layout preset and override any field:

```toml
[tool.inspect-evals-lint]
preset = "template"   # or "monorepo" / "register"
```

<!-- config-table:start -->

| Key                         | `template` preset                                            | `monorepo` preset                                            | `register` preset                                            | Meaning                                                                                                                                                                                                                                                                                                               |
| --------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `source-root`               | `src`                                                        | `src/inspect_evals`                                          | `src`                                                        | Directory (relative to the repo root) holding one sub-directory per evaluation.                                                                                                                                                                                                                                       |
| `tests-root`                | `tests`                                                      | `tests`                                                      | `tests`                                                      | Directory holding `<tests_root>/<name>/` test packages.                                                                                                                                                                                                                                                               |
| `tests-layout`              | `per-eval`                                                   | `per-eval`                                                   | `flat`                                                       | `per-eval` requires `<tests_root>/<name>/`; `flat` also accepts test files directly under `tests_root`, as single-evaluation repositories usually have.                                                                                                                                                               |
| `readme-location`           | `eval-dir`                                                   | `eval-dir`                                                   | `repo-root`                                                  | `eval-dir` requires `README.md` inside the evaluation directory; `repo-root` also accepts the repository's top-level `README.md`.                                                                                                                                                                                     |
| `eval-yaml-required`        | `true`                                                       | `true`                                                       | `false`                                                      | Whether a missing `eval.yaml` fails. False skips instead, for repositories whose metadata lives in the inspect_evals register; a present file is still validated.                                                                                                                                                     |
| `import-prefix`             | `""`                                                         | `inspect_evals`                                              | `""`                                                         | Dotted import prefix for evaluations, e.g. `inspect_evals`; empty when an eval imports as `<name>`.                                                                                                                                                                                                                   |
| `registry`                  | `entry-points`                                               | `module`                                                     | `entry-points`                                               | How tasks are registered: a Python module that imports every eval, `[project.entry-points.inspect_ai]`, or not checked.                                                                                                                                                                                               |
| `registry-module`           | unset                                                        | `src/inspect_evals/_registry.py`                             | unset                                                        | Path of the registry module (relative to the repo root); required when `registry == "module"`.                                                                                                                                                                                                                        |
| `helper-dirs`               | `["utils"]`                                                  | `["utils"]`                                                  | `["utils"]`                                                  | Sub-directories of `source_root` holding shared code rather than an evaluation. They are linted with the helper scope: the rules that guard code behaviour (private imports, score values, model roles, dependencies, tests for custom components) but not the ones about an evaluation's structure and registration. |
| `ignore-dirs`               | `["examples"]`                                               | `[]`                                                         | `["examples"]`                                               | Sub-directories of `source_root` that are never linted.                                                                                                                                                                                                                                                               |
| `eval-yaml-required-fields` | `["title", "description", "group", "contributors", "tasks"]` | `["title", "description", "group", "contributors", "tasks"]` | `["title", "description", "group", "contributors", "tasks"]` | Top-level keys every `eval.yaml` must define.                                                                                                                                                                                                                                                                         |
| `isolated-packages-dir`     | unset                                                        | `packages`                                                   | unset                                                        | Directory of per-eval `<dir>/<name>/pyproject.toml` files that declare an eval's dependencies instead of a root extra.                                                                                                                                                                                                |
| `select`                    | `["IE"]`                                                     | `["IE"]`                                                     | `["IE"]`                                                     | Rules to run: names, codes or code prefixes. The default prefix selects every rule.                                                                                                                                                                                                                                   |
| `ignore`                    | `[]`                                                         | `[]`                                                         | `[]`                                                         | Rules never to run, in the same forms as `select`. Wins over `select`.                                                                                                                                                                                                                                                |
| `exclude`                   | `[]`                                                         | `[]`                                                         | `[]`                                                         | Glob patterns, relative to the repository root, of files the AST-based rules never read. For code that is shipped into a sandbox rather than run on the host, such as challenge sources that are not even valid Python 3.                                                                                             |
| `per-file-ignores`          | `[]`                                                         | `[]`                                                         | `[]`                                                         | `(glob, selectors)` pairs: findings in files matching the glob are suppressed for the selected rules.                                                                                                                                                                                                                 |
| `allowlists.<rule>`         | `{}`                                                         | `{}`                                                         | `{}`                                                         | Per rule, the `(package, key)` pairs it reports as warnings instead of failures. Only rules declared with `allowlist=True` accept one.                                                                                                                                                                                |
| `<rule>`                    | `{}`                                                         | `{ dockerfile_locking = { host-lock-coupling = "warn" } }`   | `{}`                                                         | `[tool.inspect-evals-lint.<rule>]` tables, passed through to the rule that declares them. Keys may be kebab-case; a preset's default for a key applies when the table leaves it unset.                                                                                                                                |

<!-- config-table:end -->

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

[tool.inspect-evals-lint.dockerfile_locking]                # a rule's own options
host-lock-coupling = "allow"
```

An allowlisted finding is reported as a warning with its message prefixed `Allowlisted:`, and an entry that no finding matches is itself a warning at `pyproject.toml`, so the list can only shrink. A table named after a rule holds that rule's options, merged over the preset's defaults key by key; each rule's page lists what it accepts. Removed keys (`disabled-checks`, `non-eval-dirs`, `sandbox-image-allowlist`, `model-role-allowlist`) are rejected with a message naming their replacement.

Only Python packages are linted: a sub-directory of `source-root` without an `__init__.py` (a README left behind after a move, a data directory) is skipped by `--all` and reported as a skip when named directly, so it needs no `ignore-dirs` entry. `ignore-dirs` is for packages you really do not want checked.

### Helper packages

Shared code that evaluations import, such as inspect_evals' `utils` package, is not an evaluation but does most of the same things: it grades, it resolves models, it imports third-party packages. Directories listed in `helper-dirs` are linted with the checks that guard that behaviour (`private_api_imports`, `score_constants`, `unscored_reason`, `get_model_location`, `model_role_resolution`, `sample_ids`, `task_overridable_defaults`, `sandbox_image_pinning`, `external_dependencies`, `tests_init` and the `custom_*_tests` checks) and not with the ones about an evaluation's structure and registration (`main_file`, `init_exports`, `readme`, `registry`, `eval_yaml`, `tests_exist`, `e2e_test`, `record_to_sample_test`). Two checks adapt: `external_dependencies` requires a helper's module-level third-party imports to be in `[project].dependencies`, since every evaluation that imports the helper loads them, while imports inside a function, a `try` block or an `if TYPE_CHECKING:` block only need declaring in some group or isolated package; and the `custom_*_tests` checks look for a helper's tests anywhere under `tests-root`, not only in `tests/<name>/`. Allowlists are keyed by package name, so `utils = ["grader"]` under `allowlists.model_role_resolution` works for a helper too. A failing helper check fails the run like any other.

The `register` preset is for an upstream repository listed in the [inspect_evals register](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/register/README.md): one evaluation, tests directly under `tests/`, the README at the repository root, and metadata held by the register entry rather than an `eval.yaml` in the repo. Run it from outside the repo with `inspect-evals-lint --root <clone> --preset register --all --output-format json`.

## For agents and tools

The documentation site is built to be read by machines as well as people:

- [`llms.txt`](https://inspect-evals-lint.generality.org/llms.txt) is an index of the whole site in the [llms.txt](https://llmstxt.org) convention; [`llms-full.txt`](https://inspect-evals-lint.generality.org/llms-full.txt) is every page concatenated.
- Every page is also served as raw Markdown at the same path with a `.md` suffix, for example [`rules/IEBP002.md`](https://inspect-evals-lint.generality.org/rules/IEBP002.md).
- [`rules.json`](https://inspect-evals-lint.generality.org/rules.json) lists every rule with its code, name, category, scopes, summary and page URLs.
- Locally, `inspect-evals-lint --explain <code>` prints the same page text, `--list-rules --output-format json` lists the rules, and `--output-format json` gives findings with file, line, column and a hint ([schema](docs/output.md)).

## Suppressing a finding

Every finding has a file and, where it is in a file's contents, a line, so one comment syntax covers every rule:

- Line: `# inspect-evals-lint: ignore[IEBP003]` on the offending line, or on any line of a multi-line statement (formatters move trailing comments inside parenthesised imports). Names, codes and code prefixes are accepted, comma-separated. In a Dockerfile, whose instructions take no trailing comment, put it on the line above the instruction.
- File: `# inspect-evals-lint: ignore-file[IEBP003]` within the first ten lines of the file.
- Paths: `per-file-ignores` in the configuration table, for whole directories.
- Not linted at all: `exclude`, for code that is shipped into a sandbox rather than run on the host.

The comment is namespaced with the tool's name rather than reusing ruff's `# noqa`: ruff reads every `# noqa` comment and warns about codes it does not know, so a shared spelling would turn each suppression into a ruff warning. Suppressed findings still appear in reports, marked `[suppressed]`, and count as passing. A marker the linter does not read suppresses nothing and is reported as a warning by `suppression_syntax` (IECQ005) with the replacement in its hint: the former `noautolint` comments and `.noautolint` files, a bare `ignore` or `ignore[]`, an `ignore-file` past the header, or a selector that names no rule.

## Checks

[docs/CHECKS.md](docs/CHECKS.md) lists every rule by category and links to a page per rule under [docs/rules/](docs/rules/), generated from each rule's docstring by `python -m inspect_evals_lint.docs`; `inspect-evals-lint --explain <code>` prints the same text. Each rule is a decorated function in `src/inspect_evals_lint/rules/`: adding one means adding the function, its docstring and its tests, and regenerating the docs (pre-commit checks they are current).

## Python API

```python
from pathlib import Path
from inspect_evals_lint import lint_repository, load_config

root = Path(".")
run = lint_repository(root, load_config(root))      # RunReport: every evaluation and helper package
for package in run.packages:
    print(package.name, package.kind, package.passed(), package.summary())
    for d in package.diagnostics:
        print(f"  {d.rule.code} {d.location}: {d.message}")
```

`lint_package(root, name, config)` lints one package and returns a `PackageReport`. `lint_task_files(root, ["src/castle/castle.py"], PRESETS["register"])` lints the packages holding task files (one per distinct layout, see `task_layout`) and raises `UnsupportedLayoutError` for a bare module. `report.score()` and `run.score()` give a `Score`: every rule counted once at the worst status it reported (`fail`, `warn`, `suppressed`, `pass`, `skip` in that order), with `passing` (pass + warn), `applicable` (everything but skip) and a per-category split, the numbers the register badges show. `render_markdown(run, source_link=...)` produces the Markdown summary, with locations linked wherever `source_link(path, line)` returns a URL. `rules()` lists every registered `Rule`; `get_rule("IEBP002")` or `get_rule("model_role_resolution")` looks one up. `run.to_dict()` is the JSON document described in [docs/output.md](docs/output.md).

## Development

```bash
uv sync
uv run pre-commit install   # optional: run the lint stack on every commit
uv run pytest                 # per-rule tests live under tests/rules/
uv run basedpyright src
```

`docs/CHECKS.md`, `docs/rules/`, `docs/index.md` and the configuration table above are generated by `uv run python -m inspect_evals_lint.docs`; pre-commit fails if they are out of date. `uv run --group docs mkdocs serve` previews the site, which `docs.yml` publishes from main. Linting (ruff, [zizmor](https://docs.zizmor.sh/), mdformat) runs via [pre-commit](https://pre-commit.com); CI runs the same stack plus basedpyright and pytest via the shared [`python-ci`](https://github.com/Generality-Labs/python-project-template) reusable workflow.

## Releasing

See [RELEASING.md](RELEASING.md).
