# Checks

Every check produces one of `pass`, `fail`, `warn`, `skip` or `suppressed`. Only `fail` makes the run exit non-zero. Names in parentheses are what you pass to `--check` and write in suppressions.

## File structure

- The evaluation directory exists at `<source-root>/<eval_name>/` (`eval_location`). Every other check depends on this one.
- `<eval_name>.py` or `tasks.py` exists and defines at least one `@task` function (`main_file`). Keeping tasks in a predictably named module lets tooling and readers find them. When both files exist, the first that defines a task is used, `<eval_name>.py` first.
- `__init__.py` exports every `@task` function from the main file, via `__all__` or `from ... import` (`init_exports`).
- The evaluation is registered so `inspect eval` can find it (`registry`): under `[project.entry-points.inspect_ai]` in `pyproject.toml` (`registry = "entry-points"`), or imported by a registry module (`registry = "module"`). Set `registry = "none"` to skip.
- `eval.yaml` exists, is a mapping, and defines the configured required fields (`eval_yaml`). With `eval-yaml-required = false` a missing file is a skip, because the inspect_evals register entry carries the metadata for upstream repositories; a present file is still validated.
- `README.md` exists (`readme`); it warns if the file still contains `TODO:` markers. With `readme-location = "repo-root"` the repository's top-level `README.md` is accepted when the evaluation directory has none.

## Code quality

- No imports from private `inspect_ai` modules, meaning any dotted segment starting with `_` (`private_api_imports`). Private modules change without notice. One result per import site, so line-level suppression works.
- `Score()` calls use the `CORRECT` / `INCORRECT` constants rather than the string literals `"C"`, `"I"`, `"CORRECT"`, `"INCORRECT"` (`score_constants`).
- Every `Score.unscored(...)` call passes a `reason=` that is not `None` or empty, and the legacy `"unscored_reason"` metadata key does not appear (`unscored_reason`). `Score.reason` (inspect_ai 0.3.261) is the first-class record of why a sample was left unscored, read by metrics and log tooling, and it supersedes the interim `metadata["unscored_reason"]` convention. Only attribute calls such as `Score.unscored(...)` count, so a locally defined metric named `unscored()` is not mistaken for the constructor; docstrings mentioning the old key are ignored. One result per site.
- Third-party imports are declared in `pyproject.toml` (`external_dependencies`). An import counts as external when it is not in the standard library, not in `[project].dependencies`, not a module local to the evaluation and not the repository's own package. Each must appear in some `[project.optional-dependencies]` group or `[dependency-groups]` entry (other than `dev`), or in the isolated package's `pyproject.toml` when `isolated-packages-dir` is set. An evaluation with external imports must also own a group named after itself unless it is isolated. Import-to-distribution mapping uses the packages installed in the current environment plus a few static aliases, so results depend on the environment the linter runs in. Names are compared in [PEP 503](https://peps.python.org/pep-0503/#normalized-names) normalised form, so `inspect_ai`, `inspect-ai` and `Inspect.AI` all match.

## Tests

- A test directory exists at `<tests-root>/<eval_name>/` (`tests_exist`). With `tests-layout = "flat"`, test files directly under `<tests-root>/` are accepted when that directory is absent, as single-evaluation repositories usually have.
- The test directory and every sub-directory contain `__init__.py` (`tests_init`). Per-evaluation test trees with duplicate module basenames collide during pytest collection without them. Skipped when the tests live directly under the tests root, where there is nothing to collide with.
- At least one test file calls `eval()` or `eval_async()` (or an alias imported from `inspect_ai`) and mentions `mockllm/model` (`e2e_test`). An end-to-end run against the mock model catches wiring mistakes without spending tokens.
- If the evaluation mentions `record_to_sample`, some test file does too (`record_to_sample_test`).
- Every `@solver`, `@scorer` and `@tool` function name appears somewhere in the test tree (`custom_solver_tests`, `custom_scorer_tests`, `custom_tool_tests`). This is a presence check, not a quality check.

## Best practices

- Every `get_model(role=...)` call resolves deliberately: an explicit model, a pinned `default=`, or `required=True` (`model_role_resolution`). A role with none of these silently falls back to the model under evaluation when it isn't bound at invocation, so a grader can grade its own output and the scores still look plausible. A literal `default=None`, `model=None` or `required=False` changes nothing at runtime and so does not count. One result per call site, so line-level suppression works. Entries in `model-role-allowlist` (`{ eval = ["role"] }`, with `"<dynamic>"` for a non-literal role name) warn instead of failing so an existing surface can be burned down while new violations are blocked; a stale entry also warns so it gets removed. Contributed to inspect_evals by @antnewman.
- `get_model()` is only called inside `@solver` or `@scorer` functions (`get_model_location`, warns). Resolving models late keeps tasks declarative and lets callers override the model. One result per call site.
- Every `Sample(...)` call passes `id=` (`sample_ids`). Stable IDs keep samples comparable across shuffles, reruns and dataset updates.
- `@task` parameters whose names contain `solver`, `scorer`, `metric`, `metrics`, `grader` or `model` have defaults (`task_overridable_defaults`), so the task runs unconfigured and each piece can still be overridden.
- Registry-pulled images in `compose*.y*ml` files use an immutable tag or an `@sha256` digest (`sandbox_image_pinning`). Untagged and `:latest` references fail because a registry push silently changes the evaluation environment. Services with `build:` and `${VAR}` interpolated images are skipped. Entries in `sandbox-image-allowlist` warn instead of failing; a stale entry also warns so it gets removed.

## Suppression

- Line: `# noautolint: <check_name>`
- File: `# noautolint-file: <check_name>` within the first ten lines
- Directory: `<subdir>/.noautolint` listing check names; files beneath are also excluded from AST-based checks
- Evaluation: `<eval_dir>/.noautolint` listing check names

Suppressed results still appear in reports, prefixed with `[suppressed]`, and count as passing.
