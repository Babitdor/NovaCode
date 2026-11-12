# NovaCode 1.2.12

## Fix Windows input after `/update`

- Keep a lightweight terminal launcher alive across update and session-resume
  handoffs. The invoking shell now waits until the resumed Nova exits, instead
  of competing with its composer and mouse input for the same console.
- Continue exiting the heavy agent runtime before installation, releasing its
  native extension files for the updater. The waiting launcher uses only the
  Python standard library and does not load agent or third-party native modules.
- Preserve session identity, interrupted-turn continuation, repeated updates,
  Ctrl+C handling and exit statuses. Headless, pipe and session-worker commands
  bypass the interactive launcher; POSIX restart behavior is unchanged.
- For the first restart from an older Windows release that already returned its
  console to the shell, open the resumed Nova in a fresh interactive console.
  Later updates preserve that console through the launcher.
- Stop an updater if its handoff cannot be registered, avoiding an untracked
  update after reporting failure.

## Verification

- Reproduced early shell return with an isolated Windows pseudo-console.
- Verified composer typing, mouse scrolling and a clean shell return across two
  real Windows console restart chains, without installing updates or calling a
  model provider. Added this regression to Windows updater and cross-platform CI.
- Revalidated updater, release-note, session-continuation, team-handoff and
  startup behavior: **111 focused tests passed**, including the native console
  regression. Ruff and Mypy checks passed. No new runtime dependency is added.

## Install or recover an affected session

Close the frozen terminal, open a fresh one, then run:

```text
uv tool upgrade novacode-cli
nova --continue
```

For a pinned installation, or to select optional integrations:

```text
uv tool install --python 3.11 --force "novacode-cli==1.2.12"
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.12"
```
