# Background task responsiveness — 2026-11-03

## Startup follow-up — 2026-11-08

Nova now reserves the async server URL at boot and validates user graphs when
background delegation is first requested. Validation still excludes broken graphs
before the server starts. Concurrent starts share one validation; cancellation,
shutdown and replaced plans cannot publish a stale server. Restarting after all
user graphs are removed restores the packaged configuration. External servers
are probed only when their declared workspace matches this session.

Startup token counting uses known local OpenAI counters or a local approximation;
it does not call Anthropic's token-count API or invoke remote/custom counters.
Approximate counts are not stored as native counts. Existing native cache entries
remain usable. MCP discovery has an async cancellation deadline and releases the
caller without waiting for slow worker cleanup.

Two alternating baseline/candidate pairs on Windows, using the installed Python
environment and warm filesystem caches, measured preparation medians of **7.90 s
and 5.83 s** (26% lower). Async planning fell from **3.24 s to 0.0014 s**. Some
necessary SDK imports move from planning into the main build, so the planning
reduction is larger than the total improvement. Baseline token-count requests
were intercepted; the candidate attempted none. These measurements exclude actual
remote latency, session restore and first-frame rendering.

No dependencies, model selection or execution permissions changed. The first
background delegation now pays the deferred graph-validation cost.

## Earlier background-task measurements

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

Linux verification used WSL Ubuntu and the locked Python 3.11 environment. Optional native search engines were not installed for this check, so the Linux workflow's separate mandatory native-engine coverage was not reproduced. Windows installer/update jobs were not run. Live network validation was performed subsequently, as recorded below. These checks support this performance pass; they do not establish whole-product production readiness.

## Live network validation of 49a14ad

The performance implementation was pushed to `main` as `49a14ad5af061e36b40df566f8b27f0d83a903e1`, then tested over real sockets. No production code changes were needed during this validation.

### Real LangGraph server

An isolated LangGraph development server (API 0.14.3) ran on WSL Ubuntu using the existing locked `agents-server` extra in a disposable environment. It used eight workers and a deterministic async graph, with no model calls or tools. Clients made actual LangGraph SDK HTTP requests over loopback. Instrumentation wrapped the SDK to count requests and clients; it did not replace network responses. The watcher used its production polling interval, request timeout and concurrency limit. These measurements cover the local server transport and monitoring lifecycle, rather than a deployed WAN agent cluster.

For 64 already-completed runs, every returned graph state was validated before comparing baseline `994d5a6` against candidate `49a14ad` in separate processes. This is one live comparison; the repeated offline measurements above remain separate.

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Completion notifications delivered | 64/64 | 64/64 |
| Time to deliver all notifications | 6,452.50 ms | 253.81 ms |
| Maximum heartbeat delay | 423.17 ms | 18.58 ms |
| SDK clients created / closed | 64 / 64 | 1 / 1 |
| Peak concurrent status requests | 8 | 8 |
| Status-request p95 | 188.14 ms | 21.19 ms |

The notification-time improvement includes removal of the initial six-second delay. It does not mean the graph jobs themselves executed faster.

Additional live cases passed:

- **16 changing runs:** all 16 terminal notifications arrived, including 15 expected successes and one intentional graph failure; all 15 successful graph states were validated. Monitoring issued 32 status reads, used one client and closed it. Notifications completed in 6,037.30 ms; the subsequent six-second polling interval remains in effect.
- **Cancel eight active observations:** monitoring closed in **1.47 ms**, all eight remote jobs remained active, the client closed, and all in-flight reads were cancelled. The harness then explicitly cancelled only its own eight test jobs.
- **Disconnect/recovery:** a local forwarding proxy physically dropped three connections, then forwarded the next request to the real LangGraph server. Both interruption and recovery notices appeared, followed by the correct completion; the client closed. Production six-second retry spacing made this case take 18.02 s.
- **Stalled socket:** a TCP fixture accepted the real SDK connection without returning a response. A one-second remote wait timed out in **1,002.46 ms**, cancelled its read and closed its client.
- **Input during stalled I/O:** interrupting the observation released it in **0.57 ms**, left zero active waiters and closed the second client. The remote task was not cancelled by the waiter.

All test threads were deleted, owned test runs were cancelled where necessary, and the isolated server shut down. No existing user agent server, session, remote chat or repository content was used as a load target.

### External CommandCode HTTPS requests

The exact requested route was `commandcode:deepseek/deepseek-v4-flash`, with **no model fallback**. Nova's actual model adapter loaded the existing credential and preserved the configured privacy/ZDR setting. Only a constant synthetic canary prompt was sent; the test bound output to 128 tokens and disabled client SDK retries. Upstream retry behavior is not observable.

- One streaming smoke request passed in **2,019.12 ms**.
- A six-request burst at concurrency two passed **6/6** exact-output checks, including three streaming and three non-streaming calls. Request p95 was **1,773.42 ms**; streaming first-content p95 was **1,766.40 ms**. The burst finished in 4,287.83 ms.
- The burst observed a **152.01 ms** maximum heartbeat delay. A separate two-request diagnostic with profiling and asyncio debugging passed both output checks and observed **38.66 ms**, without reproducing the larger delay. The diagnostic adds overhead and is not a directly comparable performance sample. The original 152 ms outlier remains recorded; its cause was not established, and no whole-application latency guarantee is claimed.
- Total external requests: **9/9 valid responses**, using **333 input tokens and 198 output tokens**, including smoke and diagnostic calls. Exact dollar cost was not calculated because route-specific billing rates were not verified.

This was a bounded live transport and concurrency check. Long-duration soak, saturation, production-cluster authentication/tenant isolation, native OpenAI/Anthropic routes, and model-backed coding-workflow quality were not tested in this pass.

Local verification artifacts are under `.tmp/live-network/`: `agent-results.json`, `provider-smoke.json`, `provider-results.json`, `provider-diagnostic.json`, `provider-profile.txt`, `summary.json`, and the isolated harness scripts. They are not committed; the reports contain numeric/status metadata without credential values or repository prompts.
