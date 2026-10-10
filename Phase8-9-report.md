# Phases 8–9: terminal responsiveness and memory lifecycle

Changes are local and preserve command/tool/approval and pipe contracts.
No live LLM call was made. Offline tests use fake models and real terminal
renderers or subprocess pipes.

## Terminal behavior

- The interactive Rich renderer now displays streamed text instead of ignoring
  `TextDelta`. A bounded transient preview paints the first fragment immediately,
  then refreshes at most once per 100 ms when new text arrives. There is no
  background redraw thread for this preview. Final Markdown replaces it.
- Request feedback starts before ancillary Vixie I/O, which now has a one-second
  timeout. Redirected text output retains its final-message behavior; headless
  stream-json and session JSONL retain their flush/short-write handling.
- Renderer finalization stops both status and preview on exceptions/cancellation,
  restores temporary approval state and closes its event source with a bounded
  wait. Ctrl+C/cancellation and exceptions are tested with the real Rich renderer.
- The existing Textual first-fragment rendering, 100-ms token/tool-group updates,
  50-ms live tool-output batching, incremental tabs and background replay remain.
  A 2,000-event burst across three hidden tabs still causes three tab refreshes.
- Unmount now releases observers even when normal quit was bypassed, stops the
  watchdog, detaches owned remote-status callbacks, clears pending tool display
  output and shuts down spawned sessions. Late tool-output callbacks are dropped.
  A loop-closing race explicitly closes its unsubmitted coroutine.
- Child environments request unbuffered Python output. Exited children explicitly
  close stdin writers; worker-owned fallback output descriptors close on exit,
  while caller-owned descriptors remain caller-owned. Stderr drains in bounded
  chunks, so a 9-MiB line without a newline cannot wedge a child.
- Final output stays concise. Existing debug/verbose paths and opt-in numeric
  diagnostics remain available; reasoning is not added to normal console output.

## Memory findings and changes

Three mount/unmount cycles exposed process-global callbacks retaining closed
apps. They also exposed `gc.freeze()` freezing the mounted app/widget cycles.
The higher GC thresholds remain; freezing is removed. Unmount stops watchdog
threads that otherwise retain the loop until its eventual close.

Watchdog history now retains at most 32 stalls, four stack samples per stall and
16,384 characters per sample. Live preview tails keep at most 65,536 characters.
Clearing a preview releases chunks directly instead of allocating an unused
joined string. Existing TUI ropes, display tails, transcript pruning, tool
offload and bounded queues are retained.

The repository audit found one existing semantic index, validated against current
content/ignore rules and reset on workspace changes. No second index/cache was
added. Phase 4–5's new parsed caches have explicit payload/entry/process bounds.
Profiles did not justify caching schema extraction or request serialization.
Conversation history/checkpoints remain governed by existing compaction and
offload policies rather than arbitrary message deletion. Historical message-ID
deduplication sets still grow with a long session; they contain identifiers,
not source/tool payloads. This remains a monitoring point rather than changing
replay semantics without task-level evidence.

## Measurements

`scripts/benchmark_phase89.py` mounts the real app three times with the same
offline workload, collecting traced peak/retained allocations and working set.
`--baseline-observers` reproduces the previous observer retention and GC freeze;
other code is shared. Baseline and candidate run in separate processes. The final
GC check occurs after event-loop shutdown. These are small lifecycle samples,
not peak memory measurements of a live coding task or semantic-index workload.

| Measurement | Baseline | Candidate |
|---|---:|---:|
| Closed apps surviving final GC | 3 | 0 |
| Tool-output callbacks retained | 3 | 0 |
| Frozen objects | 231,089 | 0 |
| Traced peak allocation | 24.34 MiB | 19.36 MiB |
| Traced retained allocation | 22.81 MiB | 14.05 MiB |
| Process working set | 136.33 MiB | 130.50 MiB |

The observed peak was 20.5% lower in this pair. The decisive retention evidence
is zero surviving apps/callbacks/frozen objects; a general memory/latency gain is
not claimed from one pair. The final candidate had only the main thread left.

The tab benchmark measured 28.64 → 29.66 ms for the 2,000-event burst, identical
31.25-ms CPU samples and three tab refreshes in both runs. Switch latency was
6.26 → 11.11 ms and maximum event-loop delay 126.18 → 86.98 ms. These single samples
are noisy and do not establish a responsiveness improvement.

Artifacts: `phase89-baseline.json`, `phase89-candidate.json`,
`phase89-tabs-baseline.json`, `phase89-tabs-candidate.json`.

## Verification

- The focused terminal/resource/pipe/headless selection passed **182 tests**;
  subsequent final streaming/supervisor/worker checks passed **52 tests**.
  The broader TUI selection completed with **259 passed and five failed**.
- Broader failures are two timer tests inspecting a callback name that is now
  wrapped by the existing pane timer, a remote-manager fake missing `bridges`,
  and two remote-child fakes not accepting `auto_approve`. These failures are in
  pre-existing/concurrent timer and routing paths, which this change does not
  modify. A separate clean baseline execution of these five is not claimed.
- JUnit artifacts: `phase89-focused.xml`, `phase89-final.xml` and
  `phase89-tests.xml`. Raw logs are under `.tmp/phase89-*.log`.
- New modules/scripts/tests pass Ruff; new production modules pass strict Mypy.
  Whole touched legacy modules still report 825 lint findings and 181 type
  findings, including existing/concurrent changes; those counts are not asserted
  to be regressions from this work. `git diff --check` passes.
- Python 3.11 syntax validation passed for 52 changed/new files. Runtime checks
  used Python 3.13 because the local configured 3.11 executable is missing.
- The full-suite attempt and live-evaluation limits are documented separately in
  `Phase4-5-report.md`; full-suite success is not claimed.

## Reproduction

Use an environment matching `uv.lock`; this workspace used
`.tmp/optimization-env/Scripts/python.exe` for runtime checks.

```powershell
python scripts/benchmark_phase89.py --repetitions 3 --baseline-observers --output phase89-baseline.json
python scripts/benchmark_phase89.py --repetitions 3 --output phase89-candidate.json
python scripts/benchmark_tabs.py --tabs 4 --events 2000 --history 100
python scripts/verify_phase45.py --tests tests/test_phase89.py tests/test_session_supervisor.py tests/test_session_worker.py tests/test_session_protocol.py tests/test_resource_efficiency.py tests/test_tab_performance.py tests/test_pipe_mode.py tests/test_tui_cancel.py tests/test_tui_output_log.py tests/test_shell_output_responsiveness.py tests/test_headless.py --output phase89-focused.xml
python scripts/verify_phase45.py --full --output full-tests.xml
```
