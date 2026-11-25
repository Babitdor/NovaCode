# NovaCode 1.2.80

## Headless: subagent observability (Ralph, Nora, Serena, task dispatches)

The agent loop yields the full subagent lifecycle, but both headless
renderers dropped it — third parties could not see that a profile dispatched
workers, that one errored, or that one was waiting for input. The lifecycle
is now surfaced:

- **`stream-json`** (one-shot): `subagent_task` (upsert by `task_id` —
  status, subagent type, label, phase, model, error, duration),
  `subagent_preview` (live "just did X" lines), `subagent_activity`
  (dispatch/completion cards) and `subagent_compaction` lines.
- **Pipe**: the same three frames (`subagent_task` / `subagent_preview` /
  `subagent_activity`), each stamped with `request_id` of the prompt that
  spawned the work.

A third-party controller can now rebuild the dynamic-agents-panel state the
TUI shows: which profiles dispatched, what each worker is doing live, and
which finished or failed.
