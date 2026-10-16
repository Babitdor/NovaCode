# Responsiveness experiments — 2026-10-16

Ten committed attempts were measured on `autoresearch/oct16`; one application change survived. History replay now allows up to two events per batch instead of four, retaining the existing 8 ms time budget, render lock, ordering, and 5 ms cooperative delay. No interface design or feature was removed.

The original three-run median maximum loop delay was **267.05 ms**. The final retained revision measured **210.18 ms**, an observed **21.3% reduction**. A fresh three-run original-revision control measured **299.14 ms**, making the final paired reduction **29.7%**. These are local workload measurements, not a guarantee against freezes or external provider latency.

## Method

- Starting application revision: `0e0a52c3bae25b7404605dc7844421f7c2cda15c`.
- Frozen isolated harness: `23a7d5c6c8b6869a76431669127adbb2af63d3a3`.
- Retained performance commit: `552a5baa8d990fed3092091f5ebc6daccd3f5bd7`.
- Five-layout regression coverage commit: `2382588`.
- Interpreter: installed uv Python 3.11.13, Windows AMD64. `--no-sync` reused the installed environment without dependency resolution or installation.
- Primary workload: `uv run --no-sync python scripts/benchmark_tabs.py --tabs 4 --events 2000 --history 100`.
- Additional workload: `uv run --no-sync python scripts/benchmark_idle.py --sessions 2 --seconds 5`.
- Each measurement group contains three sequential streaming processes. The initial baseline and experiment-1 control include three idle invocations; later groups include one two-process idle invocation. Each individual workload and its measurement boundaries stayed fixed.
- Both benchmarks use temporary configuration and layout paths, disabled update checks, no voice warmup, no git branch worker, and fake agents. The idle harness previously read personal configuration; that was corrected before the accepted baseline.
- A preliminary baseline overlapped the initial regression tests. Its files named `baseline-*` are excluded from all accepted comparisons; `baseline-clean-*` is the accepted baseline. Subsequent benchmark groups ran without concurrent tests or profilers.
- Initial command allowance was 300 seconds; subsequent benchmark subprocesses had a 60-second timeout, the minimum required bound. No accepted benchmark timed out. Regression tests used `--timeout=300`.
- Attempts were committed before measurement and discarded with a reset to the last retained commit. Tracked files were initially clean. The five untracked reference images were preserved.
- A candidate needed at least 5% primary improvement, another three candidate runs, comparison with the prior best revision, passing focused tests, and no reproducible additional-metric or behavior regression.

## Attempts

The metric column uses confirmation results when available. Initial candidate and repeated-control values appear in the explanation. Later candidates were compared with the most recent measurement of the same retained revision (166.08 ms in `exp4-control`), rather than treating the older, slower 251.09 ms sample as a new opportunity.

| Attempt | Commit | Median max delay (ms) | Outcome | Explanation |
|---:|---|---:|---|---|
| 1 | `8ac489b` | 249.25 | discard | One-event replay: initial 217.25; control 261.02; confirmation 249.25, only 4.51 percent; 11 tests passed |
| 2 | `552a5ba` | 251.09 | keep | Two-event replay: initial 185.13; confirmation 251.09; control 544.06 noisy; 5.98 percent below original baseline; 11 tests passed |
| 3 | `0f83a62` | 472.94 | discard | Prepare body before mount: 472.94 ms, primary and burst/switch worse; no acceptance |
| 4 | `617793c` | 234.85 | discard | 16ms replay yield: initial 175.95; control 166.08; confirmation 234.85; 11 tests passed |
| 5 | `909ef01` | 178.02 | discard | 2ms budget: 178.02 vs latest same-best-revision control 166.08; 11 tests passed |
| 6 | `2209251` | 258.93 | discard | Eight-event batches: 258.93 vs latest best control 166.08; 11 tests passed |
| 7 | `c9ad8e9` | 333.35 | discard | Hidden-preview lock bypass: 333.35; no primary improvement; 11 tests passed |
| 8 | `b859283` | 262.51 | discard | Batch prune scheduling: 262.51; tab tests 11 passed; long-run byte-budget tests 5 passed |
| 9 | `5e12fc2` | 282.11 | discard | Cached decoder fields: 282.11; 11 tab and 33 protocol tests passed; no primary improvement |
| 10 | `acfdb0a` | 249.79 | discard | Zero-delay replay yield: 249.79 vs latest best control 166.08; 11 tests passed |

Experiment 2 initially measured 185.13 ms and confirmed at 251.09 ms, 6.0% below the original 267.05 ms baseline. Its immediate control was unusually slow (544.06 ms), so that control alone was not used to justify retention. The final independent comparison reproduced an improvement at 210.18 ms versus a fresh 299.14 ms original-revision control. Experiment 1 failed its confirmation threshold (4.5%); experiment 4 lost to its fresh control. All ten attempts completed the 11 tab-performance tests. Experiment 8 also passed the five long-run tests; experiment 9 passed the 33 wire-protocol tests.

## Raw streaming measurements

All values are milliseconds. Each primary sample is the maximum heartbeat delay of a separate benchmark process; the primary statistic is the median of those three maxima. Burst, switch, and readiness columns are medians of the same runs.

| Group | Max delay run 1 | Run 2 | Run 3 | Primary median | Burst median | Switch median | Readiness median |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline-clean | 272.15 | 267.05 | 265.22 | 267.05 | 56.76 | 91.48 | 8502.66 |
| exp1 | 166.08 | 281.77 | 217.25 | 217.25 | 35.95 | 67.78 | 6507.33 |
| exp1-control | 264.28 | 261.02 | 258.44 | 261.02 | 60.94 | 89.26 | 7510.14 |
| exp1-confirm | 249.25 | 248.78 | 388.87 | 249.25 | 54.87 | 91.86 | 8818.82 |
| exp2 | 214.09 | 185.13 | 164.44 | 185.13 | 38.23 | 55.87 | 6947.65 |
| exp2-control | 234.73 | 780.85 | 544.06 | 544.06 | 45.06 | 83.92 | 7716.30 |
| exp2-confirm | 182.88 | 251.09 | 258.03 | 251.09 | 52.87 | 72.68 | 8009.50 |
| exp3 | 472.94 | 227.75 | 634.12 | 472.94 | 124.49 | 207.55 | 7546.52 |
| exp4 | 173.31 | 175.95 | 180.55 | 175.95 | 39.44 | 62.36 | 6623.09 |
| exp4-control | 161.19 | 175.80 | 166.08 | 166.08 | 36.92 | 57.28 | 6049.55 |
| exp4-confirm | 166.37 | 234.85 | 279.53 | 234.85 | 64.03 | 90.57 | 6874.75 |
| exp5 | 175.60 | 178.02 | 285.02 | 178.02 | 37.84 | 63.57 | 6870.73 |
| exp6 | 242.25 | 258.93 | 476.74 | 258.93 | 61.17 | 98.62 | 10607.45 |
| exp7 | 501.62 | 248.63 | 333.35 | 333.35 | 57.03 | 81.38 | 8636.52 |
| exp8 | 291.80 | 181.47 | 262.51 | 262.51 | 63.39 | 69.48 | 8318.22 |
| exp9 | 282.11 | 271.74 | 316.57 | 282.11 | 47.08 | 87.95 | 8832.29 |
| exp10 | 249.79 | 277.80 | 240.32 | 249.79 | 51.27 | 77.29 | 7172.75 |
| final-control | 346.83 | 299.14 | 297.79 | 299.14 | 75.98 | 100.36 | 7300.68 |
| final-confirm | 206.65 | 255.96 | 210.18 | 210.18 | 43.40 | 78.57 | 6830.23 |

Timing noise is substantial. In particular, this replay-only change cannot cause improvements in readiness or in the preceding token burst; differences in those columns demonstrate uncontrolled timing variation. The acceptance rule was applied to repeated measurements, but these sample sizes do not establish a general statistical performance bound.

## Raw idle measurements

Each row is a five-second sample from one of two concurrently idle processes. All samples retained ten status ticks and zero rain ticks; the default static banner remained rendered. CPU is percent of one core; RSS is the reported working set.

| Group | Invocation/process | CPU % | RSS MiB | Delay p95 ms | Delay max ms | Delay mean ms |
|---|---|---:|---:|---:|---:|---:|
| baseline-clean | 1/1 | 1.56 | 117.66 | 12.000 | 27.000 | 9.480 |
| baseline-clean | 1/2 | 4.67 | 118.06 | 12.000 | 27.000 | 9.554 |
| baseline-clean | 2/1 | 3.12 | 117.80 | 12.000 | 12.000 | 9.039 |
| baseline-clean | 2/2 | 2.50 | 119.79 | 12.000 | 42.000 | 9.175 |
| baseline-clean | 3/1 | 4.38 | 117.54 | 12.000 | 27.000 | 8.413 |
| baseline-clean | 3/2 | 5.31 | 117.20 | 12.000 | 27.000 | 9.603 |
| exp1 | 1/1 | 1.56 | 119.51 | 12.000 | 12.000 | 8.944 |
| exp1 | 1/2 | 3.43 | 120.28 | 12.000 | 12.000 | 8.844 |
| exp1 | 2/1 | 1.56 | 118.49 | 12.000 | 27.000 | 9.039 |
| exp1 | 2/2 | 4.98 | 116.88 | 12.000 | 27.000 | 9.417 |
| exp1 | 3/1 | 1.25 | 119.85 | 12.000 | 27.000 | 8.669 |
| exp1 | 3/2 | 1.25 | 120.16 | 12.000 | 27.000 | 8.850 |
| exp1-control | 1/1 | 4.06 | 120.00 | 12.000 | 27.000 | 8.883 |
| exp1-control | 1/2 | 2.18 | 119.99 | 12.000 | 27.000 | 8.416 |
| exp1-control | 2/1 | 1.25 | 117.68 | 12.000 | 27.000 | 8.724 |
| exp1-control | 2/2 | 1.56 | 118.19 | 12.000 | 27.000 | 9.249 |
| exp1-control | 3/1 | 0.94 | 118.86 | 27.000 | 27.000 | 9.534 |
| exp1-control | 3/2 | 1.25 | 117.94 | 12.000 | 27.000 | 9.219 |
| exp1-confirm | 1/1 | 2.49 | 118.41 | 12.000 | 27.000 | 8.341 |
| exp1-confirm | 1/2 | 5.61 | 118.10 | 12.000 | 27.000 | 8.632 |
| exp2 | 1/1 | 3.44 | 117.92 | 27.000 | 27.000 | 10.259 |
| exp2 | 1/2 | 1.56 | 118.09 | 12.000 | 27.000 | 8.850 |
| exp2-control | 1/1 | 2.19 | 120.32 | 12.000 | 12.000 | 8.989 |
| exp2-control | 1/2 | 8.44 | 121.16 | 12.000 | 27.000 | 9.337 |
| exp2-confirm | 1/1 | 1.56 | 116.55 | 26.000 | 27.000 | 9.145 |
| exp2-confirm | 1/2 | 2.81 | 116.79 | 12.000 | 27.000 | 9.152 |
| exp3 | 1/1 | 6.54 | 120.31 | 12.000 | 27.000 | 8.753 |
| exp3 | 1/2 | 4.98 | 120.61 | 12.000 | 27.000 | 9.520 |
| exp4 | 1/1 | 2.81 | 118.25 | 12.000 | 27.000 | 8.762 |
| exp4 | 1/2 | 0.94 | 117.69 | 12.000 | 27.000 | 8.785 |
| exp4-control | 1/1 | 4.05 | 118.90 | 12.000 | 27.000 | 9.386 |
| exp4-control | 1/2 | 1.25 | 118.78 | 12.000 | 27.000 | 9.061 |
| exp4-confirm | 1/1 | 1.25 | 118.53 | 12.000 | 27.000 | 9.665 |
| exp4-confirm | 1/2 | 1.87 | 118.52 | 12.000 | 27.000 | 8.989 |
| exp5 | 1/1 | 5.31 | 118.69 | 12.000 | 12.000 | 8.304 |
| exp5 | 1/2 | 2.50 | 118.88 | 12.000 | 12.000 | 8.091 |
| exp6 | 1/1 | 5.00 | 119.87 | 12.000 | 27.000 | 8.989 |
| exp6 | 1/2 | 3.12 | 120.16 | 12.000 | 27.000 | 8.762 |
| exp7 | 1/1 | 7.19 | 117.62 | 12.000 | 27.000 | 8.779 |
| exp7 | 1/2 | 3.43 | 119.80 | 12.000 | 27.000 | 9.322 |
| exp8 | 1/1 | 0.62 | 119.14 | 12.000 | 27.000 | 8.290 |
| exp8 | 1/2 | 1.87 | 118.04 | 12.000 | 27.000 | 8.536 |
| exp9 | 1/1 | 6.56 | 118.36 | 27.000 | 183.000 | 12.879 |
| exp9 | 1/2 | 5.31 | 118.47 | 27.000 | 199.000 | 13.312 |
| exp10 | 1/1 | 0.93 | 116.77 | 12.000 | 12.000 | 8.464 |
| exp10 | 1/2 | 1.56 | 117.16 | 12.000 | 12.000 | 9.511 |
| final-control | 1/1 | 2.49 | 119.84 | 12.000 | 27.000 | 8.883 |
| final-control | 1/2 | 4.38 | 119.91 | 12.000 | 27.000 | 9.237 |
| final-confirm | 1/1 | 2.81 | 118.21 | 12.000 | 12.000 | 8.966 |
| final-confirm | 1/2 | 4.05 | 119.75 | 12.000 | 12.000 | 9.039 |

The final paired idle mean CPU is 3.43% per core for the candidate versus 3.44% for the control. Candidate working sets are 118.21/119.75 MiB versus 119.84/119.91 MiB. The p95 scheduling delay is 12 ms for both. No reproducible additional-metric regression was observed.

## Verification

- Initial focused regression group: 62 passed, one failed. `tests/test_tui_cancel.py::test_escape_stops_a_running_turn` timed out waiting five seconds for the fake agent to start, before the cancellation assertion. This failure occurred on the unchanged application and is also documented in `.nova/NOVA.md`.
- Every attempt: 11 tab-performance tests passed, covering background token handling, spinner updates, rapid switching, replay ownership/order, live events behind history, bounded buffers, output-pipe fairness, and replay cancellation.
- New five-layout regression: Dashboard, Classic, Centered, Banner, and Minimal pass with an 80-message transcript, typed prompt input, complete streamed text and tool output, clean next-turn state, and modal opening/dismissal.
- Updated five-layout test plus the known cancellation test: five passed; the same pre-existing startup timeout failed again.
- Ruff passes for the changed benchmark and new regression test. Both files pass the format check. `app.py` has the same 724 Ruff findings as the original application, with zero new diagnostics. No unrelated cleanup was applied.
- Narrow mypy (`--follow-imports=skip scripts/benchmark_idle.py`) reports one pre-existing ctypes `_fields_` assignment annotation error at line 54; the original file reproduces it. A broader `--follow-imports=silent` check exceeded 300 seconds and was stopped. The application change only changes an integer argument and adds no type surface.
- Full-suite coverage: all 4,190 collected tests were attempted. The first process exited at the 300-second timeout in `test_ctrl_b_background.py::test_bang_command_background_handoff_keeps_process_and_finishes` after 783 completed tests. The remaining 3,406 tests completed in a journaled second process (3,394 passed, 12 failed, plus a session teardown error). Combined: **4,159 passed, 13 skipped, one unexpected pass, 16 failed, one timed out**, plus the configuration-guard teardown error.
- The session configuration guard detected a change to the final `home_style` field while the broad suite ran. Its first-difference offset and before/after representations uniquely identified `centered` becoming `classic`. A reviewed, hash-checked restoration returned the value to `centered`, preserving every preceding byte and all credentials. Subsequent checks use temporary profiles seeded from the restored configuration.
- Unrelated `/stop` edits appeared in the shared working tree after the measurements. They were preserved and excluded from the performance commits. The second suite segment collected at 23:12, before those source edits at 23:18. Baseline rechecks use a separate detached checkout, without switching or resetting the shared tree. The lint comparison covers the retained performance source.

### Baseline failure classification

The broad suite was not green. Fifteen failing cases and the timeout reproduced on the original application (`23a7d5c`) in a detached checkout with temporary profiles. These are the reproduced failures:

| File | Reproduced failing cases |
|---|---|
| `test_async_general_purpose.py` | `test_prompt_prefers_async_general_work_and_yields_while_pending` |
| `test_context_snapshot.py` | Both `test_empty_checkpoint_keeps_tools_and_system_baseline` parameters; `test_compaction_snapshot_counts_only_summary_and_retained_tail` |
| `test_learning_recall.py` | `test_one_task_renaming_a_skill_does_not_fill_the_ledger` |
| `test_session_reply_panels.py` | Markdown/collapse, attach/draft preservation, and explicit open-button navigation cases |
| `test_skill_versioning.py` | Patch snapshot, rollback, history, and soft-delete cases |
| `test_text_selection.py` | `test_part_of_a_markdown_reply_can_be_selected` |
| `test_tui_app.py` | `test_responsive_breakpoints_follow_width_and_height_changes` |
| `test_tui_crash_autosave.py` | `test_prompt_saved_before_stream_and_completed_state_saved` |
| `test_ctrl_b_background.py` | The same bang-command handoff case exceeded 300 seconds on both revisions |

`test_manual_update_screen_links_to_release_notes` passed in isolation on both the original and retained revisions. `test_idempotent`, which received the session-wide configuration-guard teardown error in the broad run, also passed on both isolated revisions. Those two checks finished with **2 passed** on the retained result. The writer responsible for the broad-run preference change was not isolated; its verified pre-check value was restored before further tests. No reproducible behavioral regression attributable to the two-event replay limit was found.

## Profile and remaining bottlenecks

The existing `--profile` option was used separately from scored runs. In the diagnostic recording, Textual `_refresh_layout` accumulated 2.540 seconds, render updates 1.220 seconds, and replay 0.826 seconds. Cold imports and filesystem lookups also dominate readiness. The recording includes profiler overhead and is not comparable numerically to scored latency.

The retained adjustment bounds the amount of work initiated in one replay batch. A single expensive layout, render, or synchronous callback can still stall the shared event loop. Further work should isolate layout/style invalidation on long transcripts and profile individual expensive callbacks under real input and tool-output traffic. Phase-specific delay and total replay duration would improve future measurements. More invasive virtualization or caching would need explicit correctness and visual checks; the smaller experiments here did not justify those changes.

## Local artifacts and scope

`results.tsv`, append-only `run.log`, the cProfile recording, complete raw JSON, per-attempt tests, and full-suite output are locally excluded through `.git/info/exclude` under `research-data/`. This report includes the accepted raw measurements so they remain reviewable in the branch. Discarded commits are identified above and remain in the local reflog. No push, new dependency, paid model call, or reference-image edit was performed. The configuration preference change detected during broad testing was restored with all other file bytes preserved. Unrelated working-tree edits remain untouched.
