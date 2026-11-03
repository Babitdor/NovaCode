# Background task responsiveness — 2026-11-03

Baseline: main commit 994d5a6. Candidate: the accompanying performance changes.
Measurements: Windows, CPython 3.11.13, the locked development environment.

## Changes

- A watcher owns one HTTP client per endpoint and authorization headers. Concurrent task setup shares it; different sessions and credentials stay isolated.
- Agent discovery and SDK client construction run outside the UI event loop.
- Monitoring checks status immediately, then retains the existing six-second polling interval, request timeouts, queue limits and eight-request concurrency limit.
- Poll request timeouts use the owning task. This preserves shutdown cancellation when a request completes concurrently; the fairness/shutdown regression previously reproduced a poll surviving cancellation.
- Remote waits include discovery and client setup in their existing time budget. Success, timeout, error and cancellation release HTTP resources without stopping the remote task. Expired result retrieval preserves the completed status.
- Interrupted-wait history retains at most 512 conversation IDs. Client setup that finishes after cancellation remains owned until its client is closed; cleanup has a bounded timeout.

No dependency, provider model, approval policy, execution permission or external telemetry changes.

## Offline benchmark

Run: python scripts/benchmark_background_tasks.py --tasks 64

The workload observes 64 already-completed tasks at one endpoint. It uses the installed SDK's real HTTP client constructor, fake status responses and a reproducible 100 ms discovery delay. It sends no requests, loads no user agent files and disables automatic API-key loading. The polling interval is zero in this benchmark to isolate setup and teardown rather than time the existing six-second timer.

Three baseline/candidate pairs ran in alternating order in separate processes. The table gives medians. A five-millisecond heartbeat samples event-loop delay and RSS. The benchmark retains weak client references so its cleanup instrumentation does not prolong client lifetimes.

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| SDK client constructions | 64.00 | 1.00 |
| Total observation time | 936.58 ms | 244.07 ms |
| Process CPU time | 812.50 ms | 93.75 ms |
| Maximum heartbeat delay | 875.34 ms | 13.66 ms |
| Sampled process peak RSS | 216.48 MiB | 173.97 MiB |

These results describe this offline monitoring workload, not model latency, live network throughput, whole-application memory or a guarantee that every UI frame stays below this delay. The immediate-check behavior is tested separately from the benchmark. RSS is sampled, not an operating-system high-water measurement. Other regression processes were running during the paired measurements, so wall-clock and heartbeat values include scheduler noise.

### Raw samples

| Revision | Total ms | CPU ms | Max delay ms | Sampled peak MiB | Clients |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 936.58 | 812.5 | 864.99 | 216.67 | 64 |
| baseline | 927.03 | 781.25 | 875.34 | 216.48 | 64 |
| baseline | 1268.79 | 1062.5 | 1209.4 | 215.9 | 64 |
| candidate | 294.96 | 93.75 | 15.8 | 173.61 | 1 |
| candidate | 216.21 | 46.88 | 13.66 | 174.17 | 1 |
| candidate | 244.07 | 109.38 | 13.02 | 173.97 | 1 |

## Verification

Focused checks cover coalesced setup, route/header/session isolation, missing agents, retries, cancellation during setup and cleanup, stalled client close, immediate completion, bounded waits, completed-result retrieval timeouts, bounded waiter history, concurrent polling, shell output, tabs, and shutdown.

The new source and benchmark pass strict mypy. New files pass Ruff; the touched existing files introduce no lint violations.

Verification results:

- Windows offline CI regression selection: **2,801 passed, 6 skipped**, plus 11 subtests (664.92 s).
- Linux offline CI regression selection: **2,789 passed, 12 skipped**, plus 11 subtests; six tab-completion checks failed because the temporary checkout directory was named `nova-background-ci-…` (612.01 s). The tests expected `@tab:b` to match only the backend tab, but the root project's directory name also contained `b`. All six failures reproduced with the unchanged baseline source. Re-running all tab-mention, monitoring and waiter tests from a workspace named `NovaCode` passed **54 tests**, including all six original failures (24.86 s). This gives 2,795 passing distinct cases and 12 skips across the full selection and focused rerun, rather than a single clean full Linux run. No product code or assertion was changed to conceal these fixture assumptions.
- Additional focused shutdown, tab-performance, noisy-shell-output and long-run checks: **92 passed**.
- Strict mypy: no errors in the three touched production modules and the benchmark. New files pass all configured Ruff rules; existing touched files introduce no lint diagnostics. The CI focused F/I lint selection passes all 66 targets.

Linux verification used WSL Ubuntu and the locked Python 3.11 environment. Optional native search engines were not installed for this check, so the Linux workflow's separate mandatory native-engine coverage was not reproduced. Windows installer/update jobs and live agent-server/provider-network load checks were not run. These checks support this performance pass; they do not establish whole-product production readiness.
