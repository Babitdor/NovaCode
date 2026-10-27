# NovaCode 1.1.1

## Project rooms

- Add `/rooms`, a Rooms button and Ctrl+Shift+R for project channels, agent DMs
  and a shared activity board, while retaining existing session tabs.
- Run Claude Code and Codex through their installed CLIs and existing logins,
  with separate resumable conversations and attributed replies. Nova uses the
  shared agent loop with private checkpoints and room-scoped tools.
- Persist messages, tasks, notes and read positions in the canonical project's
  ignored `.nova/workspace/` directory. Add a participant-bound local MCP bridge
  for published context, verified artifacts and structured handoffs.
- Require explicit task approval before implementation, preserve isolated Git
  worktrees, limit project execution to two workers and six initial task turns,
  and require a separate owner decision for extensions and integration.
- Keep native CLI permission controls, pause interrupted execution without
  replay, preserve work on cancellation, and refuse integration into dirty or
  changed project bases.
- Preserve room drafts, unread indicators and independent transcript scrolling;
  distinguish agent and file mention suggestions and support narrow terminals.
- Reject stale mention suggestions after typing or switching rooms, keeping
  older agent completions from replacing newer file input.
- Add explicit Markdown export of selected channel notes and display reported
  usage, marking unavailable usage as unknown.

Validation: 33 release and room checks, all 30 room cases in randomized order,
Ruff, formatting and mypy passed after the release fix; the implementation previously passed 119 focused regression
tests and 29 room cases in randomized order. Tests use fake provider responses and CLI
streams, temporary Git worktrees, real local subprocess/MCP transports and
Textual pilots. Live Claude/Codex login and provider smoke tests remain opt-in.
Broader suite failures and Windows timeouts are documented in
`PROJECT_ROOMS_EVIDENCE.md`.
