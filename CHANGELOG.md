# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial extraction of the `autolint` checks from [inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals) `tools/run_autolint/` as an installable package with an `inspect-evals-lint` CLI.
- `[tool.inspect-evals-lint]` configuration with `monorepo` and `template` layout presets covering source root, tests root, import prefix, registry mode (`module`, `entry-points`, `none`), non-eval directories, required `eval.yaml` fields, isolated package directory, disabled checks and the sandbox image allowlist.
- Repository root discovery from the nearest configured `pyproject.toml`, with `--root` and `--preset` overrides.
- Malformed `eval.yaml`, unparsable main files and unreadable test files now produce `fail` results instead of crashing the run.
