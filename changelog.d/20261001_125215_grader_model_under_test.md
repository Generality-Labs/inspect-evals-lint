### Added

- `grader_model_under_test` (IEBP013) reports an error when scorer code selects the model under evaluation with `get_model()` and uses it for `.generate(...)`. It checks calls with no model argument, calls using `state.model`, and calls using a parameter that defaults to `None`. Reading model metadata, such as `get_model().name` for a tokenizer, is allowed. Calls with `role=` remain covered by IEBP002. The rule documentation describes the supported forms, exclusions, and known limits ([#61](https://github.com/Generality-Labs/inspect-evals-lint/issues/61)).
