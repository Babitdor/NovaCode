# NovaCode v1.0.40

## Fixed

- Keep newer provider context counts after compaction and exclude auxiliary summarizer requests from the main context indicator.
- Refresh the TUI context indicators together when usage arrives. Remote goal runs now sum usage across turns while displaying the latest request's context size.
- Release `wait_for_task` when new user input arrives, allowing the agent to respond while the background shell job or subagent continues. Wait interruption is isolated by conversation and supported by the TUI, child sessions, and remote messages.

## Validation and limits

- A combined offline goal, compaction, usage, protocol, waiter, worker, remote-status, and TUI regression run passed all 205 tests.
- Waiter Ruff formatting, lint, and mypy checks passed; the wider repository retains existing lint findings.
- Live-provider behavior was not exercised. No new dependencies were added.
