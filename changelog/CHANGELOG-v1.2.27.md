# NovaCode 1.2.27

## Command Center and Rooms layout

- Keep Command Center filter labels visible when interface presets add button
  borders. Wrap filters onto additional rows on narrow terminals.
- Reflow the Rooms toolbar from the conversation's available width, including
  when navigation or member panels are open. Keep those panels mutually exclusive
  when space is limited.
- Compact Rooms headers, search and composing on short terminals so the
  transcript and multiline composer remain accessible.
- Remove message and message-action hover highlighting while retaining keyboard
  focus indicators and reaction state.

## Rooms management

- Add a channel settings menu for renaming channels, editing topics, marking all
  conversations read, clearing messages or threads, and deleting custom channels.
- Preserve channel identities, history and drafts during renames. Clearing a
  thread preserves its starter; clearing a conversation resets its agent sessions
  while preserving tasks, notes, drafts and files.
- Confirm destructive actions and prevent clearing or deletion while jobs are
  queued, working or cancelling, including jobs in another Nova session.
- Protect the default channel and agent DMs from deletion. Channel deletion
  removes associated local records but preserves project files and Git worktrees.
- Add message copying and allow the project owner to delete agent messages.
- Refresh open conversations after another session clears or deletes a channel.

See [Project Rooms](../docs/project-rooms.md) for controls and deletion behavior.
Deleting local messages does not undo completed agent work or erase provider-side
history. No new runtime dependency is required.

## Validation

Regression coverage checks all 14 interface presets with three representative
color themes at four terminal sizes, down to 40 columns by 14 rows. Rooms checks
cover transactional rollback, protected channels, worker cancellation, draft
preservation and owner controls. Live provider calls were not exercised.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.27
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.27"
```
