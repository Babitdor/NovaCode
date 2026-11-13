# Contributing to NovaCode

Thanks for your interest in contributing to NovaCode (`novacode-cli`). This document describes how to propose changes, set up a development environment, and submit pull requests.

## Before you start

- **Search first:** check existing [issues](https://github.com/Babitdor/NovaCode/issues) and pull requests before opening a new one.
- **Discuss large changes:** for significant features, refactors, or behavior changes, open an issue or start a discussion first so scope and approach can be agreed before implementation.
- **Security issues:** do **not** report vulnerabilities in public issues. Follow [SECURITY.md](SECURITY.md).
- **Code of Conduct:** participation in this project is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Ways to contribute

- Report bugs with clear reproduction steps.
- Improve documentation, examples, and developer ergonomics.
- Propose and implement focused features or fixes.

Maintainers may label well-scoped starter work as `good first issue` or `help wanted` once triage is in place.

## Development setup

NovaCode targets Python 3.11+ and is developed from this repository.

1. Clone the repository and install dependencies (including dev tools):

   ```sh
   uv sync --locked --group dev
   ```

2. Run NovaCode from source:

   ```sh
   uv run nova
   ```

3. Make focused changes that match nearby source and tests. Preserve unrelated user edits.

See [README.md](README.md), [AGENTS.md](AGENTS.md), and [`.nova/NOVA.md`](.nova/NOVA.md) for project architecture and conventions. [`.github/CI.md`](.github/CI.md) describes the checks CI runs and how to mirror them locally.

## Checks before opening a PR

Run the smallest relevant checks for your change and report what you ran. CI uses locked/offline-friendly commands; prefer `uv run --no-sync ...` where applicable.

Common focused checks (adapt to the files you touched):

- **Lint/format (targeted):**

  ```sh
  uv run --no-sync ruff check --select F,I novacode_cli/ tests/
  uv run ruff format novacode_cli/ tests/ --check
  ```

- **Focused tests:**

  ```sh
  uv run --no-sync pytest tests/test_<feature>.py -q
  ```

  Use `-k <name>` for a single test. The full suite is `uv run pytest tests/ -q` when the change or request warrants it.

Refer to [`.github/CI.md`](.github/CI.md) for the exact regression, lint, and MCP/code-mode gates run in CI.

## Pull request expectations

- Keep diffs small and scoped to the issue/request.
- Include a clear summary and link the related issue when applicable.
- Describe verification performed (commands run) and note any behavior that remains unverified.
- Update docs/changelog when user-facing behavior or release metadata changes.
- Do **not** include secrets, credentials, tokens, or sensitive personal data in code, tests, logs, screenshots, or PR descriptions.
- Match existing conventions and avoid unrelated refactors.

## Branches and commits

- Create a branch for your work and open a PR against the default branch unless maintainers request otherwise.
- Write clear commit messages describing *why* the change is needed.
- Follow repository conventions for version bumps/releases (see [`.nova/NOVA.md`](.nova/NOVA.md)); do not release or push on behalf of maintainers unless explicitly requested.

## Maintainer sign-off checklist (open-source readiness)

Before treating the repository as fully open to external contributions, maintainers should confirm:

- [ ] **Ownership/rights:** maintainers have rights to all code, docs, and assets in the repo and release artifacts (including employer/client clearance where applicable).
- [ ] **Vendored/asset provenance:** third-party material (e.g. `novacode_cli/prompts/ponytail/*`, `shared-skills/*`, `skills/*` and any bundled LICENSE files) has compatible licenses and required attribution.
- [ ] **Dependency license spot-check:** dependencies declared in `pyproject.toml`/lockfile are compatible with project licensing and distribution.
- [ ] **LICENSE attribution decision:** root `LICENSE`, `pyproject.toml` (`license`, authors), and README license statement are consistent and reflect the correct copyright holder/year.
- [ ] **Security contact:** [SECURITY.md](SECURITY.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) list concrete, monitored maintainer-owned contacts/processes (no placeholders).
- [ ] **Secret hygiene:** tracked files and git history reviewed for exposed credentials; any exposed secrets rotated/remediated (history rewrite only with coordinated maintainer action).

> **Note:** This checklist is documentation for maintainer action. Automated edits in this repo should not guess legal attribution, rotate credentials, or rewrite git history.
