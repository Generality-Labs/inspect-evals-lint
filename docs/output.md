# Output formats

`--output-format text` (the default) prints a report per package and, with more than one package, an overall summary, a per-rule compliance table and the failures grouped by rule. The other three formats are described here. Under each, progress messages go to stderr and stdout carries only the document, so the output can be redirected or piped.

## JSON (`--output-format json`)

One document per run. `RunReport.to_dict()` produces the same mapping from the Python API.

```json
{
  "schema_version": 1,
  "version": "0.3.0",
  "root": "/abs/path/to/repo",
  "passed": false,
  "summary": {"pass": 40, "fail": 2, "warn": 1, "skip": 6, "suppressed": 0},
  "score": {"pass": 38, "fail": 2, "warn": 1, "skip": 6, "suppressed": 0, "applicable": 41, "passing": 39, "score": 0.9512,
            "by_category": {"best_practices": {"pass": 8, "fail": 2, "warn": 1, "skip": 2, "suppressed": 0, "applicable": 11, "passing": 9, "score": 0.8182}}},
  "packages": [
    {
      "name": "gpqa",
      "kind": "eval",
      "passed": false,
      "skipped": null,
      "summary": {"pass": 20, "fail": 2, "warn": 0, "skip": 3, "suppressed": 0},
      "score": {"pass": 19, "fail": 1, "warn": 0, "skip": 3, "suppressed": 0, "applicable": 20, "passing": 19, "score": 0.95, "by_category": {"...": "..."}},
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

| Field                      | Meaning                                                                                                                                                                                                                                                                                                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`           | Integer, bumped when this document changes shape. Check it first.                                                                                                                                                                                                                                                                                                                     |
| `version`                  | The inspect-evals-lint version that produced the document.                                                                                                                                                                                                                                                                                                                            |
| `root`                     | Absolute repository root the run used. Every `file` below is relative to it when it falls under it.                                                                                                                                                                                                                                                                                   |
| `passed`                   | `true` when no diagnostic in any package has status `fail`. Mirrors the exit code.                                                                                                                                                                                                                                                                                                    |
| `summary`                  | Counts of every outcome and diagnostic status across the run.                                                                                                                                                                                                                                                                                                                         |
| `score`                    | Rules met out of rules applicable. Every rule that ran counts once, at the worst status it reported (`fail`, `warn`, `suppressed`, `pass`, `skip` in that order). `passing` is `pass` + `warn`; `applicable` is everything but `skip`; `score` is their ratio, or `null` when nothing applied; `by_category` repeats the counts per category. The register badges show these numbers. |
| `packages[].kind`          | `eval` or `helper`. Filter on this rather than expecting separate lists.                                                                                                                                                                                                                                                                                                              |
| `packages[].skipped`       | Why the package was not linted at all (listed in `ignore-dirs`), else `null`.                                                                                                                                                                                                                                                                                                         |
| `packages[].score`         | The package's own score, in the same shape as the run's.                                                                                                                                                                                                                                                                                                                              |
| `packages[].outcomes[]`    | One per rule that ran and had nothing to point at: `status` is `pass` or `skip`, `message` says why.                                                                                                                                                                                                                                                                                  |
| `packages[].diagnostics[]` | One per finding. `severity` is `error` or `warning`; `status` is `fail`, `warn` or `suppressed`. `line` and `column` are 1-based and `null` when the finding is about a file or directory as a whole. `hint` is what to do about it, or `null`.                                                                                                                                       |

`category` is always one of `file_structure`, `code_quality`, `tests` and `best_practices`; a new rule joins one of the four, so tooling that groups by category (badges, dashboards) does not change when rules are added.

## GitHub Actions (`--output-format github`)

One [workflow command](https://docs.github.com/en/actions/reference/workflow-commands-for-github-actions) per failing or warning diagnostic, so a lint step annotates the pull request at the right line, followed by a one-line summary:

```
::error file=src/inspect_evals/gpqa/gpqa.py,line=42,col=12,title=IEBP003 sample_ids::Sample() call without id=; pass a stable id= so the sample survives shuffles and reruns
::warning file=pyproject.toml,title=IEBP005 sandbox_image_pinning::Allowlist entry 'x/y' for sandbox_image_pinning on 'cybench' is no longer needed; remove it from [tool.inspect-evals-lint.allowlists.sandbox_image_pinning]
inspect-evals-lint: 128/130 packages passed; 2 failed, 1 warnings, 0 suppressed
```

Skips, passes and suppressed findings produce no annotation. Property values and messages are percent-escaped the way the workflow-command syntax requires.

## Markdown (`--output-format markdown`)

A summary shaped for a pull request comment or a job summary. Per package: a heading, a headline of rules met with the per-category split, then a collapsible list of the rules not met, with warnings or suppressed, each finding on its own line with its location and the hint beneath. Rules that passed or did not apply are counted but not listed. One closing line says how the counts work.

```markdown
### `gpqa`

**19/20 checks met** · Structure 6/6 · Code quality 4/4 · Tests 6/6 · Best practices 3/4

<details>
<summary>1 rule(s) not met, with warnings or suppressed</summary>

- **Not met** [`sample_ids`](https://inspect-evals-lint.generality.org/rules/IEBP003/)
  - `src/inspect_evals/gpqa/gpqa.py:42` Sample() call without id=<br>  Hint: pass a stable id= so the sample survives shuffles and reruns

</details>
```

Locations are code spans from the CLI. From the Python API, `render_markdown(run, source_link=...)` links each location wherever the callback returns a URL, for example into the repository at the linted commit, and `docs_base` redirects the rule links. Messages, hints and paths from the linted repository are escaped so they cannot open fences or break tables.
