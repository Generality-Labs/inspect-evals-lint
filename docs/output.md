# Output formats

`--output-format text` (the default) prints a report per package and, with more than one package, an overall summary, a per-rule compliance table and the failures grouped by rule. The two machine-readable formats are described here. Under either, progress messages go to stderr and stdout carries only the document, so the output can be redirected or piped.

## JSON (`--output-format json`)

One document per run. `RunReport.to_dict()` produces the same mapping from the Python API.

```json
{
  "schema_version": 1,
  "version": "0.3.0",
  "root": "/abs/path/to/repo",
  "passed": false,
  "summary": {"pass": 40, "fail": 2, "warn": 1, "skip": 6, "suppressed": 0},
  "packages": [
    {
      "name": "gpqa",
      "kind": "eval",
      "passed": false,
      "skipped": null,
      "summary": {"pass": 20, "fail": 2, "warn": 0, "skip": 3, "suppressed": 0},
      "outcomes": [
        {"rule": "package_location", "code": "IEFS001", "category": "file_structure", "status": "pass", "message": "Package located at src/inspect_evals/gpqa"}
      ],
      "diagnostics": [
        {
          "rule": "sample_ids",
          "code": "IEBP003",
          "category": "best_practices",
          "severity": "error",
          "status": "fail",
          "message": "Sample() call without id=",
          "file": "src/inspect_evals/gpqa/gpqa.py",
          "line": 42,
          "column": 12,
          "hint": "pass a stable id= so the sample survives shuffles and reruns"
        }
      ]
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer, bumped when this document changes shape. Check it first. |
| `version` | The inspect-evals-lint version that produced the document. |
| `root` | Absolute repository root the run used. Every `file` below is relative to it when it falls under it. |
| `passed` | `true` when no diagnostic in any package has status `fail`. Mirrors the exit code. |
| `summary` | Counts of every outcome and diagnostic status across the run. |
| `packages[].kind` | `eval` or `helper`. Filter on this rather than expecting separate lists. |
| `packages[].skipped` | Why the package was not linted at all (listed in `ignore-dirs`), else `null`. |
| `packages[].outcomes[]` | One per rule that ran and had nothing to point at: `status` is `pass` or `skip`, `message` says why. |
| `packages[].diagnostics[]` | One per finding. `severity` is `error` or `warning`; `status` is `fail`, `warn` or `suppressed`. `line` and `column` are 1-based and `null` when the finding is about a file or directory as a whole. `hint` is what to do about it, or `null`. |

`category` is always one of `file_structure`, `code_quality`, `tests` and `best_practices`; a new rule joins one of the four, so tooling that groups by category (badges, dashboards) does not change when rules are added.

## GitHub Actions (`--output-format github`)

One [workflow command](https://docs.github.com/en/actions/reference/workflow-commands-for-github-actions) per failing or warning diagnostic, so a lint step annotates the pull request at the right line, followed by a one-line summary:

```
::error file=src/inspect_evals/gpqa/gpqa.py,line=42,col=12,title=IEBP003 sample_ids::Sample() call without id=; pass a stable id= so the sample survives shuffles and reruns
::warning file=pyproject.toml,title=IEBP005 sandbox_image_pinning::Allowlist entry 'x/y' for sandbox_image_pinning on 'cybench' is no longer needed; remove it from [tool.inspect-evals-lint.allowlists.sandbox_image_pinning]
inspect-evals-lint: 128/130 packages passed; 2 failed, 1 warnings, 0 suppressed
```

Skips, passes and suppressed findings produce no annotation. Property values and messages are percent-escaped the way the workflow-command syntax requires.
