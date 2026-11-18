### Fixed

- `IEBP001` now checks that `get_model()` and `model_roles()` run during solver, scorer or agent execution, including calls through package helpers. Lookups in decorated factories and eager defaults now produce warnings. The guidance for `IEBP002` and `IEBP013` distinguishes custom single-model graders from built-in scorers that support grader panels and explains how to defer default-provider creation. ([#111](https://github.com/Generality-Labs/inspect-evals-lint/issues/111))
