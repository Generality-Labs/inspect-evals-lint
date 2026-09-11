#!/usr/bin/env bash
# Apply this repo's GitHub settings and rulesets: settings-as-code for the
# things GitHub keeps in the UI/API rather than in checked-in files.
#
#   1. .github/repo-settings.json is sent verbatim as the body of
#      PATCH /repos/{owner}/{repo}, so any key that endpoint accepts can be
#      set there (merge methods, delete_branch_on_merge, has_wiki, ...).
#      Full key list: https://docs.github.com/rest/repos/repos#update-a-repository
#   2. Every ruleset JSON in .github/rulesets/ is created if absent, or
#      updated in place when a ruleset with the same name already exists.
#      The JSON is the shape the GitHub UI imports and exports
#      (Settings -> Rules -> Rulesets), so it round-trips through the dashboard.
#
# Idempotent: safe to re-run after editing any of the JSON files. Needs `gh`
# authenticated as a repo admin, and `jq`.
#
# The rulesets API is plan-gated for private repos (Free plan: 403 "Upgrade to
# GitHub Pro..."). The script applies the plain settings, says so, and exits 0
# so it can sit in a setup checklist without failing it.
#
# Usage:
#   scripts/setup-repo.sh              # the repo behind the 'origin' remote
#   scripts/setup-repo.sh OWNER/REPO   # an explicit target
set -euo pipefail

command -v jq > /dev/null || {
  echo "error: jq is required (brew install jq)" >&2
  exit 1
}
gh auth status > /dev/null 2>&1 || {
  echo "error: gh is not authenticated (run: gh auth login)" >&2
  exit 1
}

# Resolve the target from 'origin' rather than gh's default-repo guess, which
# on a checkout with several remotes (a fork plus the org repo) can pick the
# wrong one and quietly reconfigure it.
if [[ $# -gt 0 ]]; then
  repo="$1"
elif origin_url="$(git remote get-url origin 2> /dev/null)"; then
  repo="$(gh repo view "$origin_url" --json nameWithOwner --jq .nameWithOwner)"
else
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi
echo "== GitHub settings for $repo =="

SETTINGS_FILE=".github/repo-settings.json"
if [[ -f "$SETTINGS_FILE" ]]; then
  echo "+ repo settings from $SETTINGS_FILE"
  gh api -X PATCH "repos/$repo" --input "$SETTINGS_FILE" > /dev/null
else
  echo "note: $SETTINGS_FILE not found; skipping repo settings"
fi

shopt -s nullglob
files=(.github/rulesets/*.json)
if [[ ${#files[@]} -eq 0 ]]; then
  echo "no ruleset files under .github/rulesets/; done."
  exit 0
fi

# One listing up front; each file is then a create (no ruleset with that
# name) or an in-place update (PUT by id), so re-runs never duplicate.
if ! existing="$(gh api "repos/$repo/rulesets?per_page=100" 2>&1)"; then
  if grep -q "Upgrade to GitHub" <<< "$existing"; then
    echo "note: the rulesets API refuses private repos on this GitHub plan"
    echo "      (needs Pro/Team+). Plain settings were applied; re-run this"
    echo "      script after a plan upgrade to apply .github/rulesets/*.json."
    exit 0
  fi
  printf '%s\n' "$existing" >&2
  exit 1
fi
for f in "${files[@]}"; do
  name="$(jq -r '.name' "$f")"
  id="$(jq -r --arg name "$name" '.[] | select(.name == $name) | .id' <<< "$existing" | head -n 1)"
  if [[ -n "$id" ]]; then
    echo "+ update ruleset '$name' (id $id) from $f"
    gh api -X PUT "repos/$repo/rulesets/$id" --input "$f" > /dev/null
  else
    echo "+ create ruleset '$name' from $f"
    gh api -X POST "repos/$repo/rulesets" --input "$f" > /dev/null
  fi
done

cat << EOM

Applied. Settings that stay manual:
  - GitHub environments and their reviewers/secrets (e.g. the 'pypi'
    environment used by publish.yml for trusted publishing)
  - required status checks only take effect once the named check has run at
    least once on the repository
EOM
