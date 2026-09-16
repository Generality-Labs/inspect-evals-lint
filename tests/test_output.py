"""Tests for the final summary output, ported from inspect_evals."""

import json
from pathlib import Path

from inspect_evals_lint import __version__
from inspect_evals_lint.models import LintReport, LintResult
from inspect_evals_lint.output import (
    print_final_summary,
    render_json,
    report_to_dict,
    reports_to_dict,
)


def make_reports() -> list[LintReport]:
    passing = LintReport(eval_name="good_eval")
    passing.add(LintResult(name="readme", status="pass", message="README.md exists"))
    passing.add(LintResult(name="registry", status="pass", message="Registered"))

    failing = LintReport(eval_name="bad_eval")
    failing.add(
        LintResult(
            name="readme",
            status="fail",
            message="Missing README.md",
            file="src/inspect_evals/bad_eval/README.md",
        )
    )
    failing.add(LintResult(name="registry", status="pass", message="Registered"))

    also_failing = LintReport(eval_name="worse_eval")
    also_failing.add(LintResult(name="readme", status="fail", message="Missing README.md"))
    also_failing.add(LintResult(name="registry", status="fail", message="Not in registry"))

    mixed = LintReport(eval_name="mixed_eval")
    mixed.add(
        LintResult(
            name="sandbox_image_pinning",
            status="warn",
            message="Allowlisted unpinned image",
            file="src/inspect_evals/mixed_eval/compose.yaml",
        )
    )
    mixed.add(LintResult(name="e2e_test", status="skip", message="No test directory found"))

    skipping = LintReport(eval_name="skippy_eval")
    skipping.add(LintResult(name="e2e_test", status="skip", message="No test directory found"))

    return [passing, failing, also_failing, mixed, skipping]


def test_lists_checks_run(capsys):
    print_final_summary(make_reports())
    out = capsys.readouterr().out
    assert "Checks run (4):" in out
    assert "readme" in out
    assert "registry" in out


def test_groups_failures_by_check(capsys):
    print_final_summary(make_reports())
    out = capsys.readouterr().out
    readme_pos = out.index("readme (2 failures)")
    registry_pos = out.index("registry (1 failure)")
    assert readme_pos < registry_pos
    assert out.index("bad_eval", readme_pos) < out.index("worse_eval", readme_pos)
    assert "Missing README.md" in out
    assert "Not in registry" in out


def test_failure_location_included(capsys):
    print_final_summary(make_reports())
    out = capsys.readouterr().out
    assert "src/inspect_evals/bad_eval/README.md" in out


def test_no_failures_section_when_all_pass(capsys):
    reports = [make_reports()[0]]
    print_final_summary(reports)
    out = capsys.readouterr().out
    assert "Checks run (2):" in out
    assert "failure" not in out.lower().replace("no failures", "")
    assert "No failures" in out


def test_groups_warnings_by_check(capsys):
    print_final_summary(make_reports())
    out = capsys.readouterr().out
    warn_pos = out.index("sandbox_image_pinning (1 warning)")
    assert warn_pos > out.index("Warnings by check:")
    assert "Allowlisted unpinned image" in out
    assert "src/inspect_evals/mixed_eval/compose.yaml" in "".join(out.split())


def test_warnings_section_after_failures(capsys):
    print_final_summary(make_reports())
    out = capsys.readouterr().out
    assert out.index("Failures by check:") < out.index("Warnings by check:")


def test_skips_are_not_detailed(capsys):
    print_final_summary(make_reports())
    out = capsys.readouterr().out
    assert "e2e_test" in out  # still listed under checks run
    assert "skip" not in out.lower()
    assert "No test directory found" not in out


def test_no_warnings_section_when_none(capsys):
    reports = make_reports()[:3]
    print_final_summary(reports)
    out = capsys.readouterr().out
    assert "Warnings by check:" not in out


def test_reports_to_dict_totals_and_pass_flag():
    data = reports_to_dict(make_reports())
    assert data["version"] == __version__
    assert data["root"] is None
    assert data["passed"] is False
    assert data["evaluations_passed"] == 3
    assert data["evaluations_total"] == 5
    assert data["summary"] == {"pass": 3, "fail": 3, "warn": 1, "skip": 2, "suppressed": 0}
    assert [e["name"] for e in data["evaluations"]] == [
        "good_eval",
        "bad_eval",
        "worse_eval",
        "mixed_eval",
        "skippy_eval",
    ]


def test_report_to_dict_keeps_result_order_and_fields():
    report = make_reports()[1]
    data = report_to_dict(report)
    assert data["passed"] is False
    assert data["results"] == [
        {
            "check": "readme",
            "status": "fail",
            "message": "Missing README.md",
            "file": "src/inspect_evals/bad_eval/README.md",
            "line": None,
        },
        {
            "check": "registry",
            "status": "pass",
            "message": "Registered",
            "file": None,
            "line": None,
        },
    ]


def test_report_to_dict_relativises_paths_under_root(tmp_path: Path):
    report = LintReport(eval_name="x")
    inside = tmp_path / "src" / "x" / "x.py"
    outside = Path("/definitely/elsewhere/x.py")
    report.add(LintResult(name="a", status="fail", message="m", file=str(inside), line=3))
    report.add(LintResult(name="b", status="fail", message="m", file=str(outside)))
    report.add(LintResult(name="c", status="fail", message="m", file="tests/x"))
    files = [r["file"] for r in report_to_dict(report, tmp_path)["results"]]
    assert files == ["src/x/x.py", str(outside), "tests/x"]


def test_render_json_is_parseable_and_newline_terminated():
    text = render_json(make_reports())
    assert text.endswith("\n")
    assert json.loads(text)["evaluations_total"] == 5
