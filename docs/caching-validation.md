# Caching validation

Baseline: `e4dad5e4565552c64f7dc4cab360f5d2d4369caa`.
Measured on Windows, 2026-11-01, using the unchanged locked Python 3.11 environment.
No dependency, model-selection, retention, or ZDR setting changed.

## Enabled behavior

Shared local embedding ownership, deterministic portable discovery, request usage
diagnostics, and same-model summary forks are enabled. `/usage` includes persisted
request attribution alongside existing provider quota information. `/context`
compares hashes of final system, tools, settings, and history. A changed hash is
an observation; the provider's cache-miss cause remains unknown.

Native search and Anthropic's additional automatic conversation breakpoint remain
disabled by the verified-route table. Live `claude-sonnet-4-6` was blocked by
insufficient credits (HTTP 400); `gpt-5.4` was blocked by quota/billing (HTTP 429).
Offline payload, permission, fallback, and reference-block contracts exercise
these candidate paths. Those contracts do not establish live quality or cost.
Existing Anthropic core/memory markers and other providers' automatic caching
remain available. No longer cache TTL or unsupported gateway field is added.

## Live CommandCode measurements

Exact route: `commandcode:deepseek/deepseek-v4-flash`; no model fallback. A two-call
calculator probe returned the expected result both times. Both requests reported
1,647 input tokens; cache reads were 0 then 1,536. Cache writes were unavailable.

A separate four-call workflow repaired `calc.py` from subtraction to addition,
added type hints after a memory change, replayed portable tool discovery, then
summarized the completed work. Tools were schemas only and were never executed.
Both variants passed all four structural output checks. The baseline used its
existing hierarchical summary call; the candidate forked the assembled prefix.
An asyncio ticker sampled delay and process RSS during each workflow.

| Measurement | Baseline | Candidate |
| --- | ---: | ---: |
| Input tokens | 7,640 | 8,566 |
| Output tokens | 947 | 519 |
| Total tokens | 8,587 | 9,085 |
| Cache-read tokens | 3,200 | 4,224 |
| Input minus reported cache reads | 4,440 | 4,342 |
| Cache-write tokens | unavailable | unavailable |
| Sum of request durations | 13,998 ms | 8,023 ms |
| Peak RSS | 133,554,176 bytes | 132,894,720 bytes |
| Maximum event-loop ticker delay | 120.68 ms | 24.48 ms |
| Structural quality checks | 4/4 | 4/4 |

These are single sequential runs with live provider caching and variable generated
summary lengths. Initial coding calls both reported zero reads; the baseline
summary's static template could already be cached. Total token use increased.
The observations do not establish general token, billing, memory, or TUI savings.
Exact cache-write prices for this route were unavailable, so no cache-adjusted
cost is reported. Existing price estimates remain labeled uncached estimates.

The local reproduction fixtures and numeric artifacts are in `.tmp/`:
`benchmark_caching_workflow.py`, `caching-workflow-baseline-cold.json`,
`caching-workflow-candidate-cold.json`, and `commandcode-caching-live.json`.
They contain fixed synthetic task fixtures and numeric measurements, not user
conversation content or credentials.

## Offline proof

Focused contracts cover concurrent embedding loads and encodes, local-only
lookup, failed-load retry, snapshot replacement, derived-cache invalidation,
partial/raw/streamed usage, cancellation, cross-task stream closure, request
record bounds, old persistence records, pagination, final provider payloads,
tool-reference preservation, inventory removal, and summary-only forks.

The checked-in CI selection runs on Linux and Windows and includes the new
caching contracts. Local results:

- Linux regression selection: **2,641 passed, 12 skipped**, 11 subtests passed.
- Windows regression selection: **2,645 passed, 7 skipped**, 11 subtests passed;
  one unrelated sign-in modal test raised Textual `OutOfBounds`. Its complete
  file passed in a subsequent **78-test** compaction/provider/UI check.
- Latest stream, usage, and headless checks on Windows: **114 passed**, including
  cancellation and closing a stream from a different task.
- Latest Linux focused run with isolated real `bfs`/`ugrep` binaries:
  **337 passed**. Ten additional Linux headless-production stdout/pipe tests
  failed; the same ten tests failed on the unchanged baseline (**40 passed,
  10 failed**). These tests are outside the checked-in CI selection. They remain
  an existing Linux limitation, rather than a verified headless-production gate.
- Prime evidence controls: **53 passed; 3/3 mutants killed**.
- Native/MCP Code Mode verification: **77 passed**, **92%** measured coverage,
  **410 passed / 2 skipped** in its regression selection, and all negative
  controls killed.
- CI's focused tool lint passed. Full Ruff comparison across touched files:
  **no introduced rule violations**; existing repository lint debt remains.
- Full strict mypy comparison across touched source files:
  **550 baseline errors, 550 candidate errors**, with no introduced errors.
  Focused diagnostics and embedding ownership modules also pass their isolated
  type checks.
- Wheel and source distribution built successfully; the wheel includes both
  new runtime modules.

Some latest focused checks ran after the broad selections to cover stream
cleanup, startup tool accounting, and type-check findings. No test assertions
for permissions or durable archives were weakened. No dependency was added;
deepagents remains below 0.8.
