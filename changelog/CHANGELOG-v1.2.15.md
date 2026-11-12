# NovaCode 1.2.15

## Fixes

- Prevent Windows `/update` from modifying an installation whose native
  dependencies are still loaded by another Nova terminal. Check before stopping
  the current turn, and again before moving launchers or running uv/pip. Show
  the blocking processes so the user can finish those sessions and retry.
- Running Nova processes share an installation lock. The updater requires
  exclusive ownership, preventing competing updates and new runtime launches
  while packages are being replaced. Windows Restart Manager also detects
  native files held by older Nova processes that do not take this lock.
- If a blocker appears after the current session exits, resume the unchanged
  installation in the same terminal. Never terminate other Nova windows.
- Refuse to start installation if the previous runtime has not exited before
  the update handoff times out.

## Verification

- All 93 focused updater checks passed, including native Windows console typing
  and scrolling after restart. Focused Ruff and Mypy checks passed.
- Regression tests cover a DLL actually loaded by a separate Windows process,
  installation-lock conflicts, refusal before any package mutation, session
  preservation, and handoff timeout. Windows CI includes these checks.
- No new runtime dependencies. Tests use isolated files; they do not upgrade
  the user's installed Nova environment.

## Update

Before upgrading from an older version, exit Nova in the other terminal windows.
Those windows can remain open. The older updater cannot gain this protection
until the new version is installed. Then use `/update` in the remaining session,
which continues in the same terminal, or install this version explicitly:

```text
uv tool install --python 3.11 --force "novacode-cli==1.2.15"
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.15"
```

When installing from the shell, exit all Nova sessions using that installation
first. External uv/pip commands do not participate in Nova's installation lock.
