# Project Rooms

Open `/rooms` or press **Ctrl+Shift+R**. Rooms are a local, persistent project
workspace for you, Nova, Claude and Codex.

## Chat

- Use the channel sidebar to create and switch channels or open an agent DM.
  Unread counts, drafts and history survive reopening Rooms.
- Press **Enter** to send and **Shift+Enter** to insert a newline. The existing
  composer preference also supports Ctrl+Enter to send. Markdown headings,
  lists, quotes, tables and fenced code blocks render in messages.
- Mention `@nova`, `@claude` or `@codex` to direct a discussion. Agent and file
  mentions have completion suggestions. Messages without an agent mention go
  to Nova; DMs always go to their bound agent.
- **Reply** quotes the selected message. **Thread** opens a separate conversation
  beneath a channel message; **Channel** returns to the channel. Thread drafts,
  model context and native agent sessions stay separate.
- Toggle the four reaction buttons to add or remove your own reactions.
  **Pin** / **Unpin** and the toolbar's **Pins** filter keep useful messages handy.
- **Edit** changes your own message without invoking an agent. **Cancel** restores
  your draft. **Delete** asks for confirmation and removes the message's text and
  reactions while preserving replies and threads. Deletion does not undo agent
  work or remove text from previously created provider sessions.
- Press **Ctrl+F** to search the current conversation. Search and pinned filters
  work together. **Ctrl+End** jumps to the latest message.

## Collaboration

The member panel reports actual queued, working, paused or blocked jobs. Clicking
an agent opens its DM. Idle does not imply that a provider is configured or reachable.

**Tasks** opens the shared board: review scope and prepare an isolated worktree
before **Start**. Existing worker limits, turn budgets, cancellation, resume,
read-only reviews, diff inspection and separate integration approval remain in force.
Opening a channel, searching, reacting or editing a message never starts an agent.

On narrow terminals, navigation and members collapse; **Channels** and **Members**
toggle them. **Ctrl+Backslash** toggles navigation; **Escape** returns to sessions
and pauses foreground room execution.

Room storage stays under `.nova/workspace/rooms.sqlite3`. Existing databases gain
message metadata and reactions without replacing old history or drafts. Older
messages retain an unknown timestamp. Rooms do not provide hosted multi-user
servers, voice/video calls, screen sharing or file uploads; `@file` references
continue to use the project workspace.
