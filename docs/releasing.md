# Releasing

Pushing a version tag publishes everything. `.github/workflows/release.yml` runs the tests and builds the sdist and wheel. Then it does three things:

1. **GitHub release.** It creates the release for the tag. The notes are that version's section of `CHANGELOG.md`, and the built files are attached.
2. **PyPI.** It publishes the package as [`mcpgw-gateway`](https://pypi.org/p/mcpgw-gateway). The import name and the command stay `mcpgw`; the PyPI name differs because `mcpgw` on PyPI belongs to an unrelated project.
3. **Container image.** It pushes `ghcr.io/invictus2076/mcpgw:<version>`, `:<major>.<minor>` and `:latest` for linux/amd64 and linux/arm64.

## One-time setup

**PyPI trusted publishing** needs no API token in GitHub:

1. Sign in at pypi.org and go to **Your account → Publishing → Add a new pending publisher**.
2. Fill in:
   - PyPI project name: `mcpgw-gateway`
   - Owner: `Invictus2076`
   - Repository: `mcpgw`
   - Workflow: `release.yml`
   - Environment: `pypi`
3. In GitHub, go to **Settings → Environments → New environment**, name it `pypi`, and optionally require your approval.

**Container image visibility.** GHCR creates the package as private on its first push. To make it public, go to **github.com/users/Invictus2076/packages/container/mcpgw → Package settings → Change visibility**.

## Cutting a release

1. Move the `## [Unreleased]` entries in `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD`.
2. Set `__version__ = "X.Y.Z"` in `mcpgw/__init__.py`. The workflow refuses a tag that doesn't match it.
3. Commit, then create an annotated tag and push both:

   ```bash
   git commit -am "Release X.Y.Z"
   git tag -a vX.Y.Z -m "mcpgw X.Y.Z"
   git push origin main vX.Y.Z
   ```

4. Watch **Actions → Release**. If the `pypi` environment requires approval, approve the deployment.

## Releases for older tags

Tags created before the workflow existed (v0.1.0, v0.1.1) don't contain it. To create their GitHub releases, go to **Actions → Release → Run workflow** and enter the tag. That path only creates the GitHub release; it never publishes to PyPI or GHCR.
