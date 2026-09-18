# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `gpu_sandbox_check` (best practices): an evaluation whose `eval.yaml` declares `metadata.requires.gpu` must list a `tasks` entry named `<eval>_sandbox_check` with `kind: maintenance`, the task that certifies its sandbox image on GPU hardware through the eval's own scorer. Skips when no GPU requirement is declared. Follows the `metadata.requires` and per-task `kind` fields added to `eval.yaml` in [UKGovernmentBEIS/inspect_evals#2470](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2470) and the check pattern from [#2469](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2469) and [#2472](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2472).

## [0.2.1] - 2026-09-18

### Fixed

- `external_dependencies` on a helper package accepts a deferred import that is declared by any isolated package under `isolated-packages-dir`, not only by one named after the helper. inspect_evals' `utils.huggingface` defers `import transformers` for `bold` and `novelty_bench`, both isolated, and 0.2.0 reported it as undeclared.

## [0.2.0] - 2026-09-18

### Added

- Helper packages. Directories listed in the new `helper-dirs` key (default `["utils"]` in every preset) are linted with the checks that guard code behaviour: `private_api_imports`, `score_constants`, `unscored_reason`, `get_model_location`, `model_role_resolution`, `sample_ids`, `task_overridable_defaults`, `sandbox_image_pinning`, `external_dependencies`, `tests_init` and the `custom_*_tests` checks. Structure and registration checks (`main_file`, `init_exports`, `readme`, `registry`, `eval_yaml`, `tests_exist`, `e2e_test`, `record_to_sample_test`) do not run for them. Before, these directories were skipped entirely, so a shared grader helper could carry the very `get_model(role=...)` fallback the linter exists to catch ([UKGovernmentBEIS/inspect_evals#2461](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2461)). `--all-evals` now includes helper packages, a helper can be named directly on the command line, and a failing helper check fails the run.
- `external_dependencies` tells module-level imports apart from imports inside a function, a `try` block or an `if TYPE_CHECKING:` block. For a helper package the former must be in `[project].dependencies`, because every evaluation that imports the helper loads them, while the latter only need declaring in some group. The evaluation rule is unchanged.
- The `custom_*_tests` checks look for a helper package's tests anywhere under the tests root, and `tests_init` skips a helper without a `tests/<name>/` directory.
- `ignore-dirs` names sub-directories of `source-root` that are never linted (default `["examples"]` in the `template` and `register` presets).
- `runner.CHECK_SCOPES` records which package kinds each check applies to, and `runner.CATEGORIES` the fixed set of four categories. `get_all_helper_names` and `PackageKind` join the public API; `LintReport.kind` says what was linted.
- `--json` documents carry `kind` per package and list helper packages under `helpers` with `helpers_total` / `helpers_passed`; `evaluations` and its counts keep their meaning.

### Changed

- A directory under `source-root` without an `__init__.py` is reported as a skip by `eval_location` when named directly, instead of failing every check. `--all-evals` already ignored such directories, so a documentation-only directory such as inspect_evals' `gdm_capabilities/` needs no configuration entry at all.
- `tests_init` no longer exempts sub-directories named `utils` inside an evaluation's test tree; the exemption was a leftover from the vendored linter.

### Removed

- The `non-eval-dirs` key. Its two meanings are now `helper-dirs` (lint as shared code) and `ignore-dirs` (never lint); the table rejects the old key with a message naming both.

## [0.1.1] - 2026-09-16

### Added

- `model_role_resolution` check: every `get_model(role=...)` call must pass an explicit model, pin a `default=` or set `required=True`, because an unbound role otherwise falls back to the model under evaluation and a grader silently grades itself. Ported from inspect_evals ([UKGovernmentBEIS/inspect_evals#2321](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2321) by @antnewman), and promoted from a warning to a failure with a `model-role-allowlist` configuration key that turns known call sites into warnings until they are fixed, the same ratchet `sandbox_image_pinning` uses.
- `unscored_reason` check: every `Score.unscored(...)` call must pass a `reason=`, and the legacy `"unscored_reason"` metadata key must not appear, now that `Score.reason` (inspect_ai 0.3.261) is the first-class record of why a sample was left unscored (see [UKGovernmentBEIS/inspect_evals#2459](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2459)).

### Fixed

- Every file read passes `encoding="utf-8"`, so `--all-evals` no longer aborts with `UnicodeDecodeError` on platforms whose default locale encoding is not UTF-8, such as Windows cp1252. Ported from [UKGovernmentBEIS/inspect_evals#2322](https://github.com/UKGovernmentBEIS/inspect_evals/pull/2322) by @antnewman.

## [0.1.0] - 2026-09-16

### Added

- Initial extraction of the `autolint` checks from [inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals) `tools/run_autolint/` as an installable package with an `inspect-evals-lint` CLI.
- `[tool.inspect-evals-lint]` configuration with `monorepo` and `template` layout presets covering source root, tests root, import prefix, registry mode (`module`, `entry-points`, `none`), non-eval directories, required `eval.yaml` fields, isolated package directory, disabled checks and the sandbox image allowlist.
- Repository root discovery from the nearest configured `pyproject.toml`, with `--root` and `--preset` overrides.
- Malformed `eval.yaml`, unparsable main files and unreadable test files now produce `fail` results instead of crashing the run.
- `register` layout preset for single-evaluation upstream repositories, built from three new configuration keys: `tests-layout = "flat"` accepts test files directly under the tests root when `<tests-root>/<eval>/` is absent (and skips `tests_init` there), `readme-location = "repo-root"` accepts the repository's top-level `README.md`, and `eval-yaml-required = false` turns a missing `eval.yaml` into a skip while still validating one that is present.
- `main_file` and `init_exports` accept `tasks.py` as the module holding the `@task` functions, as an alternative to `<eval_name>.py`.
- `--json` writes results as a single JSON document to stdout (progress goes to stderr), with paths relative to the repository root, for badges and other tooling. Each result carries its `category` (the CHECKS.md section), also available as `runner.CHECK_CATEGORIES`. `render_json`, `report_to_dict` and `reports_to_dict` expose the same in the Python API.

### Fixed

- `external_dependencies` compares distribution names in PEP 503 normalised form, so `import inspect_ai` is satisfied by `dependencies = ["inspect-ai"]`. Before, the check failed on that spelling whenever the linter ran in an environment without the package installed. Requirement strings using `!=`, `~=`, `@` or `,` are also parsed correctly.
- The notice about a missing `[tool.inspect-evals-lint]` table no longer has its brackets swallowed as console markup.
