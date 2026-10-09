# Tab performance and task isolation review

Measured on this Windows checkout with the real Textual app in headless mode,
isolated configuration, simulated stream events, and no model or network calls.
Use `python scripts/benchmark_tabs.py --tabs 4 --events 2000 --history 100`.

## Measurements

Four tabs, 2,000 events to hidden children, and 100 committed history messages:

| Measurement | Before this performance change | After (two runs) |
| --- | ---: | ---: |
| Event burst | 566.64 ms | 18.08–58.83 ms |
| Tab refreshes during burst | 2,000 | 3 |
| Tab switch handler | 4,615.21 ms | 5.20–15.18 ms |
| Maximum event loop delay across burst and history replay | 1,130.81 ms | 121.63–431.70 ms |

The switch now returns before replaying the 100 messages. Replay finishes in
small batches while input remains available. With five tabs, the same workload
took 21.79 ms for the burst and 5.12 ms for the switch, with four tab refreshes
and 101.11 ms maximum event loop delay. Machine load and Textual layout affect
these numbers; they are diagnostic measurements, not latency guarantees.

As a single-tab control, 2,000 visible deltas took 51.62 ms, triggered no tab
refreshes, and produced 27.09 ms maximum event loop delay. Unlike the multi-tab
case, that control renders live deltas and does not replay a hidden history.

## Reviewed and fixed

- Token-driven tab relayout, synchronous history replay, and pipe-reader starvation.
- Stale tab activation events undoing rapid switches.
- Delayed stream/tool callbacks repainting another pane or clearing its latch.
- Joining growing stream buffers while saving tab state.
- Folder validation on the UI thread and simultaneous launches exceeding capacity.
- Main process task logs, completion notes, notifications, and footer counts leaking into child tabs.
- Task panel controls using the main registry even when a child tab was selected.
- Escape and Ctrl+B controlling main-tab shell commands from child tabs.
- Windows killing a shell before its descendants, making later tree termination ineffective.
- Completed child sessions retaining running task indicators and worker shutdown leaving background commands alive.
- Background process startup errors reporting success.
- Task-panel polling sending all retained log tails; it now exchanges metadata and requests one bounded log tail on demand, with responses scoped to the tab and request ID.

Regression coverage includes real Windows subprocesses and descendants stopped
through Nova's `terminate_task` tool, worker shutdown, per-tab footer and panel
controls, approval preservation, Telegram topic routing/picker cleanup, images,
and notification activation. The final combined regression run passed 237 tests.
After the on-demand log change, a focused run passed 47 tests, including wrong-tab
log replies, pending log cleanup on exit, worker shutdown, and task panel controls.
These runs overlap and are not additive. Targeted F lint and `git diff --check`
passed; no new lint errors were introduced in the changed modules.

## Remaining validation

Live Telegram interaction and interactive terminal rendering need a manual smoke
test. Large transcripts still incur Textual layout/render costs, reflected in the
event loop delay above. The full repository suite previously could not collect
because the environment lacks `hypothesis`; this report does not claim a full
repository pass. Existing unrelated lint findings remain in `tui/app.py`.

The user approved committing and pushing this work to `main` after reviewing these results.
