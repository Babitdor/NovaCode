# Session loops

Use `/loop` in a local Nova terminal tab to repeat work while the session stays
open. This is separate from `/cron`, whose jobs persist across restarts.

```text
/loop 5m check CI status and fix failing unit tests
/loop 1h /review-pr 128
/loop "*/15 * * * *" check staging logs
/loop monitor the deployment until all pods are healthy
/loop 30s
/loop
/loop --ttl 2h 5m check CI status
/loop list
/loop stop <loop-id>
/loop stop
```

Fixed intervals accept seconds (`s`), minutes (`m`), hours (`h`) and days (`d`),
with a minimum of one second. Fixed loops first run after their interval; cron
loops first run at the next matching local minute. Five-field cron supports
ranges, lists, steps and Sunday as either 0 or 7. When both day-of-month and
day-of-week are restricted, either may match.

Omitting the interval creates a self-paced loop whose first iteration is due
immediately. The model must call `schedule_wakeup(delay_seconds=...)` to continue,
or `schedule_wakeup(stop=true)` when finished. Without a wakeup call a self-paced
loop stops after that iteration. The tool can also honor natural-language stop
requests; multiple loops require an exact `loop_id` outside an iteration. It
cannot create a loop or extend its expiration.

With no inline prompt, instructions are reloaded before every iteration. The
resolution order is project `.nova/loop.md`, project `.agent/loop.md` (legacy
alias), user `~/.nova/loop.md`, user `~/.agent/loop.md`, then a built-in prompt
that checks existing work for actionable maintenance. Files must be nonempty
UTF-8 and no larger than 64 KiB. An unreadable or invalid file stops the loop.
Nested `/skill-name` and `/skill:skill-name` invocations use the normal skill
resolver and run through the agent. Direct executable `--run` is unsupported.

Iterations use the owning conversation's normal agent stream, tool permissions
and approvals. They wait while it is busy, a modal is open, plan mode is active,
or user/remote work is queued. Missed ticks collapse into one due execution;
there is no catch-up queue or overlapping loop turn. Switching away from the
owning local tab pauses dispatch until it is active again. Minimizing the
terminal leaves the session running; quitting, clearing or replacing the
conversation discards its loops. Headless and spawned-process tabs do not host
session loops.

The default TTL is 72 hours; `--ttl` changes it up to a maximum of seven days.
Expiration deletes the loop and emits one final notification, without spending
tokens on another model turn. A running loop is cancelled at expiration on the
next timer tick. There are at most 16 loops per session. The one-second poll is
best effort, not a real-time execution guarantee.

`Esc` while waiting cancels that tab's loops. During a loop iteration it cancels
the turn and its recurrence. `/loop stop [id|all]` stops matching recurrences
and cancels a matching active loop turn. These actions never cancel another
tab's work. Cancellation does not undo tool side effects already completed.
