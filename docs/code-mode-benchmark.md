# Code Mode reliability benchmark

Improved Code Mode passed **12/12** final trials; original Code Mode and direct tools each passed **10/12**. No task showed an observed success regression. Token changes were mixed; these runs do not support a universal token-saving claim.

Run on 2026-11-14 using the saved `opencode/deepseek-v4.1-flash` model with high reasoning. Saved compression mode was Ponytail, level full; effective Caveman/Lite features were disabled for every variant. Preferences were unchanged. Each variant used the same task prompts, fixtures, model builder and settings. Trials ran in isolated temporary workspaces, interleaved direct → original → improved, with two repeats per task, a 20-model-turn cap, a 120-second deadline and provider retries disabled.

[Final raw measurements](benchmarks/code-mode/measurements.json) contain model responses, reported usage, tool results, dispatch outcomes and task assertions. [Captured original runtime sources](../scripts/fixtures/code_mode_baseline/) provide the original Code Mode variant; their hashes are in the measurements. The original variant loads its original QuickJS bootstrap and MCP description implementation. Interpreter state is deliberately discarded after each oversized-recovery snippet.

## Success and tokens

Token columns below separate successful and failed trials. Input totals include cached input. Failed-request usage can be incomplete after cancellation and is a lower bound. Cached usage is provider-reported; no monetary cost is estimated because verified pricing was unavailable.

| Variant | Success | Timeouts | Successful input / output | Failed input / output | Cached input, all trials |
|---|---:|---:|---:|---:|---:|
| direct | 10/12 | 0 | 175,285 / 2,215 | 22,152 / 573 | 163,200 |
| current | 10/12 | 1 | 160,224 / 3,436 | 287,096 / 13,464 | 381,696 |
| improved | 12/12 | 0 | 269,240 / 6,838 | 0 / 0 | 217,344 |

## Per-task comparison

Deltas compare only paired successful original/improved trials with the same task and repeat. Failed original recovery runs are excluded from savings calculations; their usage remains above and in the raw data.

| Task | Direct | Original | Improved | Input change vs original | Output change vs original |
|---|---:|---:|---:|---:|---:|
| native_discovery | 2/2 | 2/2 | 2/2 | -6.7% | -41.1% |
| mcp_composition | 2/2 | 2/2 | 2/2 | +3.7% | -20.9% |
| exact_reads | 0/2 | 2/2 | 2/2 | +26.5% | -1.3% |
| exact_edit | 2/2 | 2/2 | 2/2 | +22.1% | +11.9% |
| partial_failure | 2/2 | 2/2 | 2/2 | -14.3% | -27.3% |
| oversized_recovery | 2/2 | 0/2 | 2/2 | n/a | n/a |

Across the 10 paired successful trials, input changed +0.2% and output changed -19.8%. These aggregate changes depend on this task mix.

Direct tools dropped trailing newlines in both exact-read answers, so both failed exact assertions. Original Code Mode could not recover the one-shot oversized result after state loss: one trial timed out; the other returned a repeated fixture line instead of the exact final line. Improved Code Mode recovered both saved results without replaying the one-shot operation.

## Calls, retries and elapsed time

| Variant | Model round trips | Nested bridge calls | Repeated non-read fixture calls | Summed trial seconds |
|---|---:|---:|---:|---:|
| direct | 34 | 0 | 0 | 134.3 |
| current | 75 | 171 | 0 | 382.3 |
| improved | 62 | 54 | 0 | 232.8 |

Round trips include attempted model calls. Nested calls include discovery and descriptions, and are measured from completed interpreter envelopes; raw tool events and outcomes remain available. The retry counter counts repeated fixture operations other than reads, rather than assuming every follow-up snippet is a replay. Summed time includes failures. Prompt-cache warmth and stochastic outputs can affect comparisons.

## Excluded preliminary usage

Before correcting the controlled-failure fixture and nested-call counter, 24 preliminary trials were run. The fixture originally raised an exception that stopped the direct graph before model recovery; its replacement returns a model-visible error. The original counter also included outer interpreter calls. These trials are excluded from all final comparisons. Their [raw measurements](benchmarks/code-mode/preliminary.json) are retained to account for the additional API usage: 507,506 reported input tokens and 15,883 reported output tokens. There were **60 live trials in total**, including these 24 preliminary trials.

## Verification and limits

- The repository Code Mode verifier passed: 142 randomized integration checks, 92% measured coverage, lint, types, 461 surrounding regressions (two skips), and four assertion-killed negative controls.
- The installed QuickJS smoke check passed. Additional focused checks passed for search restrictions, unchanged values, MCP output schemas, atomic storage failures, Unicode serialization, state-loss recovery and existing offload caching. The expanded search/offload regression group passed 132 tests.
- Final focused reliability/benchmark/compaction checks passed; tests are registered in the existing CI manifest and verifier. No full repository suite, release, publication or version bump was performed.
- Offloads retain the existing mounted agent boundary and seven-day retention. Recovery still obeys normal file-read/intermediate-result limits. Console buffering remains separately capped; only selected results are archived.
- Two repeats per task provide directional evidence, not a general reliability or cost guarantee. Final ASCII-safe JSON archival and offload-cache hardening were verified offline; they preserve the fixture values measured live.

To rerun, choose a fresh output file (existing measurements are protected):

```powershell
.venv/Scripts/python.exe -m scripts.benchmark_code_mode --live --variants direct current improved --output reports/code-mode-new.json
```

This command uses the saved provider and incurs API usage. See [Code Mode documentation](mcp-code-mode.md) for the runtime API and recovery format.
