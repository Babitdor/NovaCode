# NovaCode 1.2.1

## PyPI releases

- Publish tagged releases through GitHub Actions using PyPI Trusted Publishing,
  without a stored API token.
- Check that the release tag, package version, runtime version, lockfile and
  changelog agree before building.
- Validate PyPI classifiers and package metadata, then upload only the wheel
  and source archive for the tagged version.
