# Continuous integration

`workflows/ci.yml` runs on pull requests, pushes to `main`, and manual dispatch.

- **Regression (Ubuntu/Windows):** installs Python 3.11 dependencies from
  `uv.lock`, checks release metadata and changelog presence, lints the new tool
  modules, and runs focused offline regressions for subagents, approvals, MCP,
  discovery, strict schemas, cron, and context inspection.
  Notebook editing and terminal-title activity indicators are covered too. Linux
  installs `bfs` and `ugrep` and requires real native-search checks; Windows tests
  fallback behavior without requiring these optional executables.
- **Windows install and update:** runs updater regression tests, builds the wheel
  and source distribution, installs the wheel into an isolated uv tool directory,
  and checks the `nova.exe --version` launcher before and after a forced reinstall.
  Launcher checks run outside the checkout to catch missing packaged modules.
- Test reports and built packages are uploaded as artifacts, including on failure
  when those files exist. Duplicate runs on the same ref cancel older runs.

The tests need no provider credentials. Update error paths, including locked
launchers, use controlled fixtures; the installation smoke check performs a real
wheel install and reinstall. It does not invoke `nova update` against the public
repository or reproduce a live launcher lock. Dependency installation still needs
network access.

This is a focused gate, not the full test suite or a live provider evaluation.
Linux runner results must be verified in GitHub Actions. macOS, additional Python
versions, scheduled compatibility checks, and publication can be added separately.
The workflow does not publish packages, create tags, or change repository settings.

After a successful hosted run, the three job names can be selected as required
checks in a branch ruleset. Actions use immutable commit pins and read-only
repository permissions.
