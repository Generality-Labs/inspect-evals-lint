### Added

- `fence_tolerant_json` (IECQ008) warns where `json.loads`, `model_validate_json` or `validate_json` strictly parses the `completion` of a `generate` call made in the same function. Models asked for bare JSON, even through a `response_schema`, sometimes wrap it in a code fence, and the strict parse then throws away a correct grade. The fix is `inspect_evals.utils.code.strip_json_fence`. Like IECQ006, the rule runs only where that helper is available.
