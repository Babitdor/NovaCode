# NovaCode 1.2.13

## Keep `/update` in the same terminal

- Remove the legacy fallback that opened a new Windows console after updating.
  Nova's interactive launcher always uses the existing terminal.
- Updates started from a launcher-enabled Nova session (normal launches of
  1.2.12 or later) restart and resume automatically in that same terminal,
  preserving session identity and interrupted-turn continuation.
- If an older Windows launcher has already exited and released its console to
  the shell, finish the update and display the exact resume command in the
  existing terminal. That one-time migration needs manual resume; an exited
  launcher cannot be retrofitted. No new window is opened and no competing TUI
  is started in the released console.

## Verification

- Verified typing, mouse scrolling and shell ownership through two automatic
  update/restart cycles in the same Windows pseudo-console.
- Added a regression requiring legacy recovery to print its saved-session
  resume command without spawning a console.
- All 44 focused launcher, native console, updater/resume and release-version
  checks passed, along with Ruff and Mypy.
- No new runtime dependency is required.

## Update

Use `/update` inside Nova. Launcher-enabled sessions continue automatically in
the current terminal. If the one-time legacy recovery command is shown, run it
at the prompt in that same terminal.

For a pinned installation, optional integrations remain available:

```text
uv tool install --python 3.11 --force "novacode-cli==1.2.13"
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.13"
```
