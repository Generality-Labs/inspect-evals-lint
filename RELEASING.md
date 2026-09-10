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

## Each release

1. Bump `__version__` in `src/inspect_evals_lint/__init__.py` (single source of truth — `pyproject.toml` reads it).
2. Move the `## [Unreleased]` entries in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading.
3. Commit, then tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
4. The `publish.yml` workflow builds the sdist/wheel and uploads to PyPI.
5. Create a GitHub release from the tag, pasting the changelog section.

## Sanity checks before tagging

```bash
uv run pytest
uv run basedpyright src
uv build && uvx twine check dist/*
```
