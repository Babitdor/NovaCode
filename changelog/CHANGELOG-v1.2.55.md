# NovaCode 1.2.55

## Agent loop

- A streamed answer no longer disappears. The final chunk marked its message id
  as seen *before* flushing the prose it carried, and the flush discards prose
  whose id is already seen — so on providers whose chunks share one stable id,
  the whole answer the user had just watched stream was dropped and the turn
  ended with no reply. The flush now runs first.
- A provider that delivers one whole message instead of deltas (non-streaming)
  has its answer rendered through the same guardrail, redaction and dedup path
  as streamed text. Previously such a turn committed nothing at all.
- A completed message with several text parts commits all of its parts in one
  message, and pending prose from an earlier streamed id is committed under its
  own id before a different completed message accumulates — no more merged
  "HelFinal." bubbles.

## Compaction

- Code Mode sessions can compact again. The cut point had to be a user message,
  and index 0 was excluded, so a run driven from a single user message — the
  normal Code Mode shape — always summarized an empty head and saved nothing.
  Single-user-turn sessions now cut at the overflow boundary instead, advanced
  past any tool result so no call is separated from its answer.

## Shell and background tasks

- `wait_for_task` works on the first call again. The gate counted the call's
  own tool-call row, which the running graph already includes in its state, so
  every first wait was refused with "you already waited". A wait now counts
  only once its result exists in the history — which also un-blocks two waits
  dispatched in parallel, while a genuinely repeated wait is still refused.
- The documented 10-second cap and the schema default agree; the schema offered
  30 seconds while the code clamped to 10.
- `get_task_logs` page cursors no longer drift on coloured output. Offsets
  count raw characters; the page now slices the raw join and strips ANSI for
  display, instead of slicing the stripped string against raw offsets — which
  repeated or skipped lines whenever the buffer held escape sequences. A cut
  landing inside a split escape resumes after the sequence closes instead of
  leaving orphan fragments. `tail_lines=0` returns the whole buffer on both
  paths.
- A failed background subagent wait reports the run's real error, recovered
  from the checkpoint and redacted, instead of a bare "The subagent failed."
- `terminate_task` gained `force`: a job whose drain loop died can be settled
  immediately instead of staying "running" forever with no wait resolving.

## Async subagents

- Launches keep their context. The model override from the stored `async` role,
  the owner-consent record and the auto-approve stamp were silently dropped
  whenever the launch carried any other configurable key — the run then fell
  back to the server's default model.
- A task record that can never be monitored (missing ids, or an agent spec that
  no longer resolves) is reported once as unmonitorable instead of being
  re-enqueued by every turn-end sync forever. Transient discovery failures
  still retry, three times, and the retry budget resets when the specs are
  invalidated.
- Polls in flight are no longer evicted from the dedup set, which could emit a
  second completion and a second report turn for the same task.
- A `/model` change to the `async` role invalidates the watcher's cached agent
  specs under the same lock a discovery holds, so polls cannot write stale
  specs back after the change.
- The subagent panel maps `timeout` and `interrupted` to failed instead of
  spinning them as running forever, freezes a finished row's duration instead
  of recomputing it larger on every poll, and drops rows whose task vanished
  from state.
- A completion whose report turn is cancelled is re-queued rather than lost,
  and completions that arrive while an approved plan runs are reported after
  the plan's turn settles instead of waiting for the next user message.

## Code Mode

- A subagent dispatched from a snippet returns its result. The evaluator
  rejected any state update outside `messages` and `files`, and the child's
  final state carries internal bookkeeping keys (compaction privates, caveman
  originals), so a fully successful research child died at hand-off with
  "Subagent returned unsupported state mutation" — its report visible in the
  preview while the task row read failed. Internal keys are now dropped and
  the report transfers; genuinely unknown keys still fail loudly.
- Snippet errors name the OS path when one rode on the exception, and take the
  first line of a multi-line message — Windows spawn failures carry the path
  in `filename`, which `str()` hides, so "[WinError 267] The directory name is
  invalid" could previously not be diagnosed at all.
- The shell and Python-kernel spawn paths name their working directory when a
  start fails, for the same reason.

## Steering

- Standing `/steer` rules reach subagents. The middleware was only on the main
  stack, so a constraint like "never modify test files without asking" did not
  bind the children that edit files — the block claimed to apply on every step.
- Instructions that already exist when an agent is rebuilt (model switch, MCP
  reload, plan agent) are not re-delivered as a false "just added" nudge.
- Mid-turn `/steer clear` discards queued live steers instead of resurrecting
  them as fresh turns, and remote mid-turn `/steer` uses the same
  add/list/clear handling as the TUI on both the drain and the inline path.
- The context-margin guard counts image parts, which previously weighed
  nothing, and a skipped standing block now announces itself once per skip
  streak instead of silently disappearing between 80% and the auto-compact
  threshold.
- `/steer add` and `/steer list` state the honest lifetime: the rules last for
  the session, survive `/resume`, and do not survive a restart.

## Prompt

- The persistence and verification guidance the tests pin is back: the
  Understand → Change → Verify → Leave it clean cycle with an end-state check,
  "unverifiable is a last resort", numeric-aggregation checks, the task-size
  table, and work-through-without-stopping-for-approval — all dropped in an
  earlier prompt audit while the tests kept requiring them.

## Packaging

- The release now also builds the Rust TUI client on Linux, macOS (ARM) and
  Windows and attaches each binary to the GitHub release, stamped with the
  wire-protocol version it speaks.
