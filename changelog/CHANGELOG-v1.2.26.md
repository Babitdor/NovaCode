# NovaCode 1.2.26

## Discord-style project Rooms

- Add grouped channel/DM navigation, unread counts, a searchable transcript, a
  member/activity panel and a responsive layout for narrow terminals.
- Render responses as Markdown, including headings, lists, quotes, tables and
  fenced code blocks. Attribution and reply previews remain literal text.
- Reuse the multiline prompt composer: Enter sends, Shift+Enter inserts a newline,
  and the existing Enter/Ctrl+Enter preference remains supported. Preserve channel
  and thread drafts independently, including while navigating or cancelling edits.
- Add quoted replies and separate threads with reply counts. Thread context,
  native agent sessions and agent responses stay bound to their thread.
- Add four toggleable reactions, message pins, combined search/pinned filters,
  editing of your own messages and confirmed deletion. Deleted content and reactions
  are removed locally while replies and threads remain available.
- Migrate existing SQLite room databases additively, preserving history, drafts
  and read positions. Older messages retain an unknown timestamp.
- Preserve the task board's scope review, isolated worktrees, worker/turn limits,
  cancellation, read-only reviews and separate integration approval. Browsing,
  searching, reacting and editing do not start agent work.
- Serialize transcript refreshes with navigation and protect newer drafts from
  delayed sends. Keep task controls accessible on narrower boards.

Rooms remain a local project workspace for the owner and configured agents.
Hosted multi-user servers, voice/video calls, screen sharing and file uploads
are not included. Deleting a local message does not undo completed agent work
or remove content from existing provider sessions. See
[Project Rooms](../docs/project-rooms.md) for controls and behavior.

## Consistent tool display

- Apply Code Mode's borderless status rows, themed status dots and three-line
  previews to normal-mode tool calls, with keyboard-accessible arguments and
  full output.
- Retain live command logs, background-command hints, colored file diffs and
  correct attribution for parallel and legacy ID-less events.
- Preserve ordinary tool JSON as literal output rather than interpreting it as
  a Code Mode result envelope.

## Mode-toggle contrast

- Give Prime and the other composer mode toggles an accent-tinted background and
  explicit text color on hover and keyboard focus, in enabled and disabled states.

## Validation

The Rooms, Markdown presentation, migration, thread routing, draft preservation,
message controls and toggle regressions pass locally. Agent routing checks use
simulated providers; live provider calls were not exercised. Desktop and narrow
terminal previews were inspected. New regressions are included in platform CI.
No new runtime dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.26
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.26"
```
