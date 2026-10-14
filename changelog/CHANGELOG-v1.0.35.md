# NovaCode v1.0.35

## Added

- `wait_for_task` performs a bounded wait for a background shell job or async subagent and reports its current status or result.
- `list_waiters` exposes active waits and their targets.
- The TUI task bar shows the number of active waiters and their targets, with details in a tooltip.

## Validation and limits

- Focused waiter, shell-job, prompt, and TUI tests passed. Ruff and an isolated mypy check passed.
- A live remote subagent wait was not exercised; the remote path is covered with offline tests.
