# Releasing

## One-time setup: PyPI trusted publishing

Publishing runs from GitHub Actions with no API tokens, via [trusted publishing](https://docs.pypi.org/trusted-publishers/).

1. On [pypi.org](https://pypi.org) → _Your account_ → _Publishing_ → **Add a pending publisher**:
   - PyPI project name: `inspect-evals-lint`
   - Owner: `Generality-Labs`
   - Repository name: `inspect-evals-lint`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. In the GitHub repo: _Settings → Environments → New environment_ → name it `pypi` (optionally add yourself as a required reviewer so releases need a manual approval click).

The first tagged release creates the PyPI project and converts the pending publisher into a normal one.

One more one-time step: _Settings → Actions → General_ → enable **Allow GitHub Actions to create and approve pull requests**, so the release workflow can open its pull request.

## Each release

1. _Actions_ → **Prepare release** → _Run workflow_, choosing the bump. `auto` picks minor when a changelog fragment adds, changes, deprecates or removes something, and patch when fragments only fix; it never picks major. The workflow bumps `version` in `pyproject.toml`, collects `changelog.d/` into `CHANGELOG.md`, and opens a **Release vX.Y.Z** pull request.
2. On the pull request's Checks tab, click **Approve workflows to run**. A pull request opened by Actions starts its CI in an approval-required state. Review the pull request, then push a summary paragraph under the new heading, above the first `###` section: what the release is about, and anything a consumer must do before moving their pin (a new JSON schema version, rules that newly fail). The GitHub release notes are taken from this section when the pull request merges. This repo always writes one; the template leaves it optional.
3. Merge it. `release-on-merge.yml` tags the merge commit `vX.Y.Z`, creates the GitHub release from the changelog section, and starts `publish.yml`, which checks the tag against the built wheel and uploads to PyPI.

To do the same by hand: `uv version --bump minor` (or `patch` / `major`), `uv run scriv collect`, open and merge a pull request, then `git tag vX.Y.Z && git push origin vX.Y.Z` on the merge commit. The tag push starts `publish.yml`.

## Sanity checks before tagging

```bash
uv run pytest
uv run basedpyright src
uv build && uvx twine check dist/*
```
