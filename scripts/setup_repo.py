#!/usr/bin/env python3
"""Apply this repo's GitHub settings and rulesets from files in the repo.

GitHub keeps repository settings and rulesets in the UI and API rather than in
checked-in files, so this script is the applier for two kinds of file:

1. ``.github/repo-settings.json``: keys for ``PATCH /repos/{owner}/{repo}``
   (merge methods, ``delete_branch_on_merge``, ``has_wiki``, ...). Any key that
   endpoint accepts can be set. Full list:
   https://docs.github.com/rest/repos/repos#update-a-repository
2. ``.github/rulesets/*.json``: rulesets in the shape the GitHub UI imports and
   exports (Settings -> Rules -> Rulesets). Each is created if no ruleset with
   its name exists, otherwise updated in place, so re-runs never duplicate.

Before changing anything it fetches what the repo has now and prints the
difference: settings keys whose value would change, and a unified diff of each
ruleset against GitHub's copy projected onto the keys the file sets, so ids,
timestamps and the defaults GitHub fills in never show up as changes. Nothing
is applied until you confirm, or pass ``--yes``.

The rulesets API is plan-gated for private repos (Free plan: 403 "Upgrade to
GitHub Pro..."). Settings are still applied; rulesets are skipped with a note.

Usage::

    scripts/setup_repo.py [--dry-run | --yes] [OWNER/REPO]

``OWNER/REPO`` defaults to the repo behind the ``origin`` remote (never gh's
default-repo guess, which can pick the wrong one on a multi-remote checkout).
Needs ``gh`` authenticated as a repo admin. Standard library only.

Exit codes: 0 applied or nothing to do, 1 aborted at the prompt or a GitHub
error, 2 usage error or a non-terminal run without ``--yes``.
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

SETTINGS_FILE = Path(".github/repo-settings.json")
RULESETS_DIR = Path(".github/rulesets")
PLAN_GATE_MARKER = "Upgrade to GitHub"

Json = Any
Runner = Callable[[Sequence[str], str | None], str]


class GhError(RuntimeError):
    """``gh`` exited non-zero; the message is its stderr."""


def run_gh(args: Sequence[str], stdin: str | None = None) -> str:
    """Run ``gh`` with ``args`` and return stdout; raise :class:`GhError` on failure."""
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError((result.stderr or result.stdout).strip())
    return result.stdout


def project(current: Json, want: Json) -> Json:
    """Keep only the parts of ``current`` that ``want`` also has.

    Objects are filtered by key and arrays paired by index, recursively. Leaves
    keep their ``current`` values so a diff shows what GitHub has now. An array
    element GitHub has but ``want`` lacks is kept, so it shows as a difference.
    """
    if isinstance(want, dict) and isinstance(current, dict):
        want_map = cast(dict[str, Json], want)
        current_map = cast(dict[str, Json], current)
        return {k: project(v, want_map[k]) for k, v in current_map.items() if k in want_map}
    if isinstance(want, list) and isinstance(current, list):
        want_list = cast(list[Json], want)
        current_list = cast(list[Json], current)
        return [
            project(v, want_list[i] if i < len(want_list) else None)
            for i, v in enumerate(current_list)
        ]
    return current


def canonical(value: Json) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


@dataclass
class SettingChange:
    key: str
    current: Json
    desired: Json


@dataclass
class RulesetPlan:
    path: Path
    name: str
    desired: Json
    existing_id: int | None
    diff: str  # empty when unchanged or when creating

    @property
    def changed(self) -> bool:
        return self.existing_id is None or bool(self.diff)


@dataclass
class Plan:
    repo: str
    settings: list[SettingChange] = field(default_factory=list)
    settings_file_present: bool = True
    rulesets: list[RulesetPlan] = field(default_factory=list)
    rulesets_gated: bool = False

    @property
    def any_change(self) -> bool:
        return bool(self.settings) or any(r.changed for r in self.rulesets)


def resolve_repo(explicit: str | None, gh: Runner) -> str:
    if explicit:
        return explicit
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False
    )
    view_args = ["repo", "view"]
    if origin.returncode == 0 and origin.stdout.strip():
        view_args.append(origin.stdout.strip())
    return gh([*view_args, "--json", "nameWithOwner", "--jq", ".nameWithOwner"], None).strip()


def plan_settings(repo: str, gh: Runner, root: Path) -> tuple[list[SettingChange], bool]:
    path = root / SETTINGS_FILE
    if not path.exists():
        return [], False
    desired: dict[str, Json] = json.loads(path.read_text())
    current: dict[str, Json] = json.loads(gh(["api", f"repos/{repo}"], None))
    return [
        SettingChange(key, current.get(key), value)
        for key, value in desired.items()
        if current.get(key) != value
    ], True


def plan_rulesets(repo: str, gh: Runner, root: Path) -> tuple[list[RulesetPlan], bool]:
    files = sorted((root / RULESETS_DIR).glob("*.json"))
    if not files:
        return [], False
    try:
        existing: list[dict[str, Json]] = json.loads(
            gh(["api", f"repos/{repo}/rulesets?per_page=100"], None)
        )
    except GhError as e:
        if PLAN_GATE_MARKER in str(e):
            return [], True
        raise
    by_name = {r["name"]: int(r["id"]) for r in existing}

    plans: list[RulesetPlan] = []
    for path in files:
        desired = json.loads(path.read_text())
        name = str(desired["name"])
        existing_id = by_name.get(name)
        diff = ""
        if existing_id is not None:
            current = json.loads(gh(["api", f"repos/{repo}/rulesets/{existing_id}"], None))
            diff = "".join(
                difflib.unified_diff(
                    canonical(project(current, desired)).splitlines(keepends=True),
                    canonical(desired).splitlines(keepends=True),
                    fromfile=f"{name} (current, as reported by GitHub)",
                    tofile=f"{name} ({path})",
                )
            )
        plans.append(RulesetPlan(path, name, desired, existing_id, diff))
    return plans, False


def build_plan(repo: str, gh: Runner, root: Path = Path()) -> Plan:
    settings, present = plan_settings(repo, gh, root)
    rulesets, gated = plan_rulesets(repo, gh, root)
    return Plan(repo, settings, present, rulesets, gated)


def describe(plan: Plan, out: Callable[[str], None]) -> None:
    out(f"== GitHub settings for {plan.repo} ==")
    if not plan.settings_file_present:
        out(f"  note: {SETTINGS_FILE} not found; skipping repo settings")
    elif not plan.settings:
        out(f"  repo settings: no changes ({SETTINGS_FILE} already matches)")
    else:
        out(f"  repo settings: {len(plan.settings)} change(s)")
        for change in plan.settings:
            out(f"    {change.key}: {json.dumps(change.current)} -> {json.dumps(change.desired)}")
    if plan.rulesets_gated:
        out("  note: the rulesets API refuses private repos on this GitHub plan")
        out(f"        (needs Pro/Team+); {RULESETS_DIR}/*.json skipped")
    for rs in plan.rulesets:
        if rs.existing_id is None:
            out(f"  ruleset '{rs.name}': does not exist yet; would be created from {rs.path}")
        elif rs.diff:
            out(f"  ruleset '{rs.name}' (id {rs.existing_id}): would be updated")
            for line in rs.diff.rstrip("\n").splitlines():
                out(f"    {line}")
        else:
            out(f"  ruleset '{rs.name}' (id {rs.existing_id}): no changes")


def apply(plan: Plan, gh: Runner, out: Callable[[str], None]) -> None:
    if plan.settings:
        out("+ repo settings")
        body = {c.key: c.desired for c in plan.settings}
        gh(["api", "-X", "PATCH", f"repos/{plan.repo}", "--input", "-"], json.dumps(body))
    for rs in plan.rulesets:
        if not rs.changed:
            continue
        if rs.existing_id is None:
            out(f"+ create ruleset '{rs.name}' from {rs.path}")
            gh(
                ["api", "-X", "POST", f"repos/{plan.repo}/rulesets", "--input", "-"],
                canonical(rs.desired),
            )
        else:
            out(f"+ update ruleset '{rs.name}' (id {rs.existing_id}) from {rs.path}")
            gh(
                [
                    "api",
                    "-X",
                    "PUT",
                    f"repos/{plan.repo}/rulesets/{rs.existing_id}",
                    "--input",
                    "-",
                ],
                canonical(rs.desired),
            )
    out("")
    out("Applied. Settings that stay manual:")
    out("  - GitHub environments and their reviewers/secrets (e.g. the 'pypi'")
    out("    environment used by publish.yml for trusted publishing)")
    out("  - required status checks only take effect once the named check has run at")
    out("    least once on the repository")


def main(
    argv: Sequence[str] | None = None,
    gh: Runner = run_gh,
    out: Callable[[str], None] = print,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n")[0],
        epilog="See the module docstring for details.",
    )
    parser.add_argument(
        "repo", nargs="?", metavar="OWNER/REPO", help="target repo (default: the 'origin' remote)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="show the differences and exit without applying"
    )
    mode.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="apply without asking (required when not a terminal)",
    )
    args = parser.parse_args(argv)

    try:
        gh(["auth", "status"], None)
    except GhError:
        out("error: gh is not authenticated (run: gh auth login)")
        return 1

    try:
        repo = resolve_repo(args.repo, gh)
        plan = build_plan(repo, gh)
    except GhError as e:
        out(f"error: {e}")
        return 1

    describe(plan, out)
    if not plan.any_change:
        out("Nothing to apply.")
        return 0
    if args.dry_run:
        out("Dry run; nothing applied.")
        return 0
    if not args.yes:
        if interactive is None:
            interactive = sys.stdin.isatty()
        if not interactive:
            out("Not a terminal and --yes not given; nothing applied. Re-run with --yes to apply.")
            return 2
        if ask(f"Apply these changes to {repo}? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Aborted; nothing applied.")
            return 1

    try:
        apply(plan, gh, out)
    except GhError as e:
        out(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
