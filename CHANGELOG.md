# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial extraction of the `autolint` checks from [inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals) `tools/run_autolint/` as an installable package with an `inspect-evals-lint` CLI.
- `[tool.inspect-evals-lint]` configuration with `monorepo` and `template` layout presets covering source root, tests root, import prefix, registry mode (`module`, `entry-points`, `none`), non-eval directories, required `eval.yaml` fields, isolated package directory, disabled checks and the sandbox image allowlist.
- Repository root discovery from the nearest configured `pyproject.toml`, with `--root` and `--preset` overrides.
- Malformed `eval.yaml`, unparsable main files and unreadable test files now produce `fail` results instead of crashing the run.
- `--json` writes results as a single JSON document to stdout (progress goes to stderr), with paths relative to the repository root, for badges and other tooling. `render_json`, `report_to_dict` and `reports_to_dict` expose the same in the Python API.

### Fixed

- `external_dependencies` compares distribution names in PEP 503 normalised form, so `import inspect_ai` is satisfied by `dependencies = ["inspect-ai"]`. Before, the check failed on that spelling whenever the linter ran in an environment without the package installed. Requirement strings using `!=`, `~=`, `@` or `,` are also parsed correctly.
- The notice about a missing `[tool.inspect-evals-lint]` table no longer has its brackets swallowed as console markup.
