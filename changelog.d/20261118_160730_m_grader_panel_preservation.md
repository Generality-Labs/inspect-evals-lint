### Added

- `IEBP017` (`grader_panel_preservation`) warns when `get_model(role=...)` selects one model before `model_graded_qa()` or `model_graded_fact()` can resolve a configured grader panel. It follows direct calls, local assignments and named nested functions, while allowing role-absence fallbacks, full role bindings and required-role scorers. Intentional selection of one grader can use the usual suppression with a reason. ([#112](https://github.com/Generality-Labs/inspect-evals-lint/issues/112))
