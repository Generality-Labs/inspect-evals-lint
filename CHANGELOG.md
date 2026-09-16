# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
