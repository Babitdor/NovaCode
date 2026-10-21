# NovaCode v1.0.73

## Fixed

- Updated the root uv lockfile to match the package release version so Linux and Windows CI can install with `uv sync --locked`.
- Release regression checks now verify the lockfile package version and matching changelog alongside package metadata and public version displays.

## Validation

- Checked lockfile freshness and release metadata locally; GitHub Actions is rerun on the pushed commit.
