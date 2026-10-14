# NovaCode v1.0.36

## Fixed

- The main agent is prompted to collect required async subagent results before claiming a task is complete, using bounded waits and a clear pending status after five minutes.
- Async subagents that finish during an active TUI turn are queued for an automatic result fetch after that turn.
- `!!` returns to the TUI when its terminal command exits, without waiting for an extra Enter key.

## Validation and limits

- Focused async completion, prompt, waiter, and terminal command tests passed (35 tests).
- A live remote subagent was not exercised; the remote completion path was tested offline.
