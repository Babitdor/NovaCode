# NovaCode 1.2.21

## Plugin reload and session ownership

- Fix chat failing after `/reload-plugins` with “This root already has a live
  coordinator owner.” Plugin and MCP reload now pass the conversation ID and
  reuse the model-switch rebuild path, preserving the existing coordinator,
  checkpoint, workspace, prompt, extra middleware and confinement settings.
- Preserve the current worktree when changing models, reloading plugins/MCP or
  refreshing agent definitions. Rebuilds use the active local file backend root.
- Keep the duplicate-owner guard: a different coordinator still cannot take
  ownership of a live conversation.

## Code Mode and scratch files

- Add local session scratchpads under the OS temporary directory, mounted at
  `/scratchpad/`. File write/edit approvals apply only to the calling session's
  scratchpad; explicit denies and normal shell approval remain enforced.
- Clean scratchpads on conversation clear, session exit and process shutdown.
  A retention sweep removes marked directories from dead processes after 24
  hours. New conversations receive a fresh mount and prompt.
- Clarify the active workspace, virtual file paths and shell working directory,
  including remote sandbox roots.
- Detect todo updates that only reword an unchanged status sequence without
  intervening work. Keep deterministic coalescing of parallel todo snapshots.
- Return bounded, ordered nested-call outcomes from Code Mode, with operation
  names, completion/failure/cancellation states and concise error categories.
  Queued calls cancelled before starting are distinguished from running calls.
  Failed or cancelled operations can still have external side effects.

## Validation

The combined Code Mode, approval and lifecycle selection passed 386 tests with
two skips on Windows. The reload, ownership, worktree and related selection
passed 78 tests, including a real graph/SQLite reproduction of the reload error.
Focused lint, type and whitespace checks passed. The Windows symlink escape
test requires privileges unavailable on the local test host. Existing unrelated
repository lint findings remain. No new runtime dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.21
uv tool upgrade novacode-cli
```

Optional extras:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.21"
```
