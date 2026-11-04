# Releasing

## One-time setup

### PyPI trusted publishing

Publishing runs from GitHub Actions with no API tokens, via [trusted publishing](https://docs.pypi.org/trusted-publishers/).

1. On [pypi.org](https://pypi.org) → _Your account_ → _Publishing_ → **Add a pending publisher**:
   - PyPI project name: `inspect-evals-lint`
   - Owner: `Generality-Labs`
   - Repository name: `inspect-evals-lint`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. In the GitHub repo: _Settings → Environments → New environment_ → name it `pypi`. Optionally add yourself as a required reviewer, so each publish waits for your approval.

The first release creates the PyPI project and converts the pending publisher into a normal one.

### Actions can open pull requests

_Settings → Actions → General_ → enable **Allow GitHub Actions to create and approve pull requests**, so the release workflow can open its pull request. If the box is greyed out, the organization has turned it off; an organization admin can enable it under the organization's _Settings → Actions → General_.

## Each release

1. _Actions_ → **Prepare release** → _Run workflow_, choosing the bump. `auto` picks minor when a changelog fragment adds, changes, deprecates or removes something, and patch when fragments only fix; it never picks major. The workflow bumps `version` in `pyproject.toml`, collects `changelog.d/` into `CHANGELOG.md`, and opens a **Release vX.Y.Z** pull request.
2. On the pull request's Checks tab, click **Approve workflows to run**. A pull request opened by Actions starts its CI in an approval-required state. Review the pull request, then push a summary paragraph under the new heading, above the first `###` section: what the release is about, and anything a consumer must do before moving their pin (a new JSON schema version, rules that newly fail). The GitHub release notes are taken from this section when the pull request merges. This repo always writes one; the template leaves it optional.
3. Merge it. `release-on-merge.yml` tags the merge commit `vX.Y.Z` and creates the GitHub release from the changelog section, then starts `publish.yml`, which checks the tag against the built wheel and uploads to PyPI.

To do the same by hand: `uv version --bump minor` (or `patch` / `major`), `uv run scriv collect`, open and merge a pull request, then on the merge commit run `git tag vX.Y.Z && git push origin vX.Y.Z` (the tag push starts `publish.yml`) and `gh release create vX.Y.Z` with the changelog section as its notes.

## If a step fails

- **Prepare release fails opening the pull request** with "GitHub Actions is not permitted to create or approve pull requests". Turn on the Actions setting above, then run it again.
- **Prepare release refuses.** It refuses a run from a branch other than the default, with no changelog fragments, with a fragment that has entries but no `###` heading or that quotes a scriv marker, when tag `vX.Y.Z` already exists, or while a release pull request for that version is open. Fix the fragment, close the open pull request, or, for an existing tag, choose a bigger bump. Otherwise a re-run is safe: it force-pushes its own `release/vX.Y.Z` branch.
- **The release pull request's CI never starts.** Click **Approve workflows to run** on its Checks tab. Pushing a commit to the branch yourself also starts it.
- **Something user-visible merges while the release pull request is open.** The tag goes on the merge commit, so the release ships it, but its fragment is still in `changelog.d/` and only reaches the next release's notes. Close the release pull request and run Prepare release again.
- **Release on merge fails.** Re-run the failed run from the Actions tab: it skips a tag or release that already exists. Four refusals come from the merged commit and fail again on a re-run: the branch name isn't `release/vX.Y.Z`, `pyproject.toml` at the merge commit has another version, `CHANGELOG.md` there has no `## [X.Y.Z]` section, or tag `vX.Y.Z` already points at another commit. If that tag was never released, delete it and re-run; otherwise cut a new release.
- **Publishing waits.** If the `pypi` environment has required reviewers, approve the deployment in the publish run.
- **Publishing fails.** `gh workflow run publish.yml --ref vX.Y.Z` re-runs the workflow and code exactly as they are at the tag, so it only helps when the cause is outside the repo: a PyPI outage, or a trusted publisher fixed on PyPI. A fix in the repo needs a new release: add a fragment describing it (Prepare release refuses to run without one), then run Prepare release. Do the same if the upload got through part-way, since PyPI never accepts a file twice.

## Sanity checks before releasing

Run these before **Prepare release**:

```bash
uv run pytest
uv run basedpyright src
uv build && uvx twine check dist/*
```
