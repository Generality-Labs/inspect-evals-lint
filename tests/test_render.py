"""Console summaries and the JSON document."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from inspect_evals_lint import __version__
from inspect_evals_lint.diagnostics import Diagnostic, Outcome, PackageReport, RunReport
from inspect_evals_lint.registry import get_rule
from inspect_evals_lint.render import (
    SCHEMA_VERSION,
    package_to_dict,
    print_final_summary,
    print_overall_summary,
    render_json,
    run_to_dict,
)

README = get_rule("readme")
REGISTRY = get_rule("registry")
PINNING = get_rule("sandbox_image_pinning")
E2E = get_rule("e2e_test")
assert README
assert REGISTRY
assert PINNING
assert E2E


def make_run(root: Path = Path("/repo")) -> RunReport:
    passing = PackageReport("good_eval", "eval")
    passing.add(Outcome("pass", "README.md exists", rule=README))
    passing.add(Outcome("pass", "Registered", rule=REGISTRY))

    failing = PackageReport("bad_eval", "eval")
    failing.add(
        Diagnostic(
            "Missing README.md", file=root / "src/inspect_evals/bad_eval/README.md", rule=README
        )
    )
    failing.add(Outcome("pass", "Registered", rule=REGISTRY))

    also_failing = PackageReport("worse_eval", "eval")
    also_failing.add(Diagnostic("Missing README.md", file=root / "x/README.md", rule=README))
    also_failing.add(Diagnostic("Not in registry", file=root / "pyproject.toml", rule=REGISTRY))

    mixed = PackageReport("mixed_eval", "eval")
    mixed.add(
        Diagnostic(
            "Allowlisted unpinned image",
            file=root / "src/inspect_evals/mixed_eval/compose.yaml",
            severity="warning",
            rule=PINNING,
        )
    )
    mixed.add(Outcome("skip", "No test directory found", rule=E2E))

    helper = PackageReport("utils", "helper")
    helper.add(Outcome("skip", "No test directory found", rule=E2E))

    return RunReport(root=root, packages=[passing, failing, also_failing, mixed, helper])


def test_lists_checks_run(capsys):
    print_final_summary(make_run())
    out = capsys.readouterr().out
    assert "Checks run (4):" in out
    assert "readme" in out
    assert "registry" in out


def test_groups_failures_by_check(capsys):
    print_final_summary(make_run())
    out = capsys.readouterr().out
    readme_pos = out.index("readme (2 failures)")
    registry_pos = out.index("registry (1 failure)")
    assert readme_pos < registry_pos
    assert out.index("bad_eval", readme_pos) < out.index("worse_eval", readme_pos)
    assert "Missing README.md" in out
    assert "Not in registry" in out


def test_failure_location_is_relative_to_root(capsys):
    print_final_summary(make_run())
    out = capsys.readouterr().out
    assert "src/inspect_evals/bad_eval/README.md" in out
    assert "/repo/" not in out


def test_no_failures_section_when_all_pass(capsys):
    run = make_run()
    run.packages = run.packages[:1]
    print_final_summary(run)
    out = capsys.readouterr().out
    assert "Checks run (2):" in out
    assert "No failures" in out


def test_groups_warnings_after_failures(capsys):
    print_final_summary(make_run())
    out = capsys.readouterr().out
    assert out.index("Failures by check:") < out.index("Warnings by check:")
    assert "sandbox_image_pinning (1 warning)" in out
    assert "Allowlisted unpinned image" in out


def test_skips_are_not_detailed(capsys):
    print_final_summary(make_run())
    out = capsys.readouterr().out
    assert "e2e_test" in out  # still listed under checks run
    assert "No test directory found" not in out


def test_overall_summary_counts_packages_when_helpers_are_present(capsys):
    print_overall_summary(make_run())
    out = capsys.readouterr().out
    assert "utils (helper)" in out
    assert "3/5 packages passed" in out
    run = make_run()
    run.packages = run.packages[:1]
    print_overall_summary(run)
    assert "1/1 evaluations passed" in capsys.readouterr().out


def test_run_to_dict_shape():
    data = run_to_dict(make_run())
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["version"] == __version__
    assert data["root"] == "/repo"
    assert data["passed"] is False
    assert data["summary"] == {"pass": 3, "fail": 3, "warn": 1, "skip": 2, "suppressed": 0}
    assert [(p["name"], p["kind"]) for p in data["packages"]] == [
        ("good_eval", "eval"),
        ("bad_eval", "eval"),
        ("worse_eval", "eval"),
        ("mixed_eval", "eval"),
        ("utils", "helper"),
    ]


def test_package_to_dict_fields():
    run = make_run()
    data = package_to_dict(run.packages[1], run.root)
    assert data["passed"] is False
    assert data["skipped"] is None
    assert data["rules"] == [
        {
            "rule": "registry",
            "code": "IEFS004",
            "category": "file_structure",
            "status": "pass",
            "diagnostics": [],
        },
        {
            "rule": "readme",
            "code": "IEFS006",
            "category": "file_structure",
            "status": "fail",
            "diagnostics": [0],
        },
    ]
    assert data["outcomes"] == [
        {
            "rule": "registry",
            "code": "IEFS004",
            "category": "file_structure",
            "status": "pass",
            "message": "Registered",
        }
    ]
    assert data["diagnostics"] == [
        {
            "rule": "readme",
            "code": "IEFS006",
            "category": "file_structure",
            "severity": "error",
            "status": "fail",
            "message": "Missing README.md",
            "file": "src/inspect_evals/bad_eval/README.md",
            "line": None,
            "column": None,
            "hint": None,
        }
    ]


def test_paths_outside_root_stay_absolute():
    report = PackageReport("x", "eval")
    report.add(Diagnostic("m", file=Path("/definitely/elsewhere/x.py"), line=3, rule=README))
    data = package_to_dict(report, Path("/repo"))
    assert data["diagnostics"][0]["file"] == "/definitely/elsewhere/x.py"
    assert data["diagnostics"][0]["line"] == 3


def test_skipped_package_has_no_findings():
    report = PackageReport("examples", "eval", skipped="listed in ignore-dirs")
    data = package_to_dict(report)
    assert data["skipped"] == "listed in ignore-dirs"
    assert data["rules"] == []
    assert data["outcomes"] == []
    assert data["diagnostics"] == []
    assert data["passed"] is True


def test_package_rules_count_reproduces_score():
    """Consumers can validate score by counting rules by status; indexes point at diagnostics."""
    main = get_rule("main_file")
    sample = get_rule("sample_ids")
    assert main
    assert sample

    report = PackageReport("mixed_statuses", "eval")
    report.add(Outcome("pass", "Registered", rule=REGISTRY))
    report.add(Outcome("pass", "README.md exists", rule=README))
    report.add(
        Diagnostic(
            "Missing README.md",
            file=Path("/repo/src/mixed_statuses/README.md"),
            rule=README,
        )
    )
    report.add(
        Diagnostic(
            "Sample() call without id=",
            file=Path("/repo/src/mixed_statuses/mixed.py"),
            line=10,
            rule=sample,
        )
    )
    report.add(
        Diagnostic(
            "Allowlisted unpinned image",
            file=Path("/repo/src/mixed_statuses/compose.yaml"),
            severity="warning",
            rule=PINNING,
        )
    )
    report.add(Outcome("skip", "No test directory found", rule=E2E))
    report.add(
        Diagnostic(
            "TODO marker",
            file=Path("/repo/src/mixed_statuses/main.py"),
            line=1,
            severity="warning",
            rule=main,
            suppressed=True,
        )
    )
    # Skip outcome plus a warning on the same rule: status is warn; only the warning's index.
    report.add(Outcome("skip", "No compose file", rule=PINNING))

    data = package_to_dict(report, Path("/repo"))
    assert data["rules"] == [
        {
            "rule": "main_file",
            "code": "IEFS002",
            "category": "file_structure",
            "status": "suppressed",
            "diagnostics": [3],
        },
        {
            "rule": "registry",
            "code": "IEFS004",
            "category": "file_structure",
            "status": "pass",
            "diagnostics": [],
        },
        {
            "rule": "readme",
            "code": "IEFS006",
            "category": "file_structure",
            "status": "fail",
            "diagnostics": [0],
        },
        {
            "rule": "e2e_test",
            "code": "IETS003",
            "category": "tests",
            "status": "skip",
            "diagnostics": [],
        },
        {
            "rule": "sample_ids",
            "code": "IEBP003",
            "category": "best_practices",
            "status": "fail",
            "diagnostics": [1],
        },
        {
            "rule": "sandbox_image_pinning",
            "code": "IEBP005",
            "category": "best_practices",
            "status": "warn",
            "diagnostics": [2],
        },
    ]
    for entry in data["rules"]:
        for i in entry["diagnostics"]:
            assert data["diagnostics"][i]["rule"] == entry["rule"]

    counts = Counter(r["status"] for r in data["rules"])
    score = data["score"]
    assert score["pass"] == counts.get("pass", 0)
    assert score["fail"] == counts.get("fail", 0)
    assert score["warn"] == counts.get("warn", 0)
    assert score["skip"] == counts.get("skip", 0)
    assert score["suppressed"] == counts.get("suppressed", 0)
    assert (
        score["applicable"] == score["pass"] + score["fail"] + score["warn"] + score["suppressed"]
    )
    assert score["passing"] == score["pass"] + score["warn"] + score["suppressed"]
    assert (score["pass"], score["fail"], score["warn"], score["skip"], score["suppressed"]) == (
        1,
        2,
        1,
        1,
        1,
    )


def test_render_json_is_parseable_and_newline_terminated():
    text = render_json(make_run())
    assert text.endswith("\n")
    assert len(json.loads(text)["packages"]) == 5


def test_github_annotations_cover_errors_and_warnings_only():
    from inspect_evals_lint.render import render_github

    text = render_github(make_run())
    lines = text.splitlines()
    assert lines[0] == (
        "::error file=src/inspect_evals/bad_eval/README.md,title=IEFS006 readme::"
        "src/inspect_evals/bad_eval/README.md IEFS006 readme: Missing README.md"
    )
    assert any(
        line.startswith("::warning file=src/inspect_evals/mixed_eval/compose.yaml,")
        for line in lines
    )
    assert not any("skip" in line for line in lines[:-1])
    assert (
        lines[-1] == "inspect-evals-lint: 3/5 packages passed; 3 failed, 1 warnings, 0 suppressed"
    )


def test_github_annotation_escapes_and_locates():
    from inspect_evals_lint.render.github import annotation

    run = make_run()
    d = Diagnostic(
        "50% done: a, b\nnext",
        file=Path("/repo/src/x.py"),
        line=3,
        column=7,
        hint="fix: it",
        rule=README,
    )
    assert annotation(d, run) == (
        "::error file=src/x.py,line=3,col=7,title=IEFS006 readme::"
        "src/x.py:3:7 IEFS006 readme: 50%25 done: a, b%0Anext; fix: it"
    )
    d.suppressed = True
    assert annotation(d, run) is None


def test_github_output_names_the_annotation_cap_only_when_a_run_exceeds_it():
    """The job log shows messages without file= and line=, and the panel keeps ten per level."""
    from inspect_evals_lint.render import render_github
    from inspect_evals_lint.render.github import ANNOTATION_CAP

    assert "GitHub shows at most" not in render_github(make_run())
    noisy = PackageReport("noisy_eval", "eval")
    for i in range(ANNOTATION_CAP + 1):
        noisy.add(
            Diagnostic(
                "Sample() call without id=",
                file=Path(f"/repo/src/noisy_eval/f{i}.py"),
                line=i + 1,
                severity="warning",
                rule=PINNING,
            )
        )
    text = render_github(RunReport(root=Path("/repo"), packages=[noisy]))
    assert f"GitHub shows at most {ANNOTATION_CAP} error and {ANNOTATION_CAP} warning" in text
    assert "listed above with its location" in text
    assert text.splitlines()[0].startswith("::warning file=src/noisy_eval/f0.py,line=1,")
    assert "::src/noisy_eval/f0.py:1 IEBP005 sandbox_image_pinning: Sample()" in text


def test_run_to_dict_is_available_on_the_report():
    run = make_run()
    assert run.to_dict() == run_to_dict(run)
