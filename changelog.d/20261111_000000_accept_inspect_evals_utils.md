### Fixed

- `shared_code_extraction` (IECQ006) and `fence_tolerant_json` (IECQ008) now run in a repository that depends on `inspect-evals-utils`, which ships `extract_code_block` and `strip_json_fence` in `inspect_evals_utils.code`. Before, they ran only where a helper package defined the function or `pyproject.toml` depended on `inspect-evals`, so a standalone repository using the packaged helpers was skipped. The hints name `inspect_evals_utils.code` and mention the `inspect_evals.utils.code` re-export ([#109](https://github.com/Generality-Labs/inspect-evals-lint/pull/109)).
