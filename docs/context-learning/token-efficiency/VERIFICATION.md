# Verification record

The token replay passes its 10% aggregate / 5% per-case gate: 251,140 → 123,094
estimated input tokens, identical across three sequential repetitions. The corpus
is synthetic and observation-heavy; it does not establish live task success.

## Focused checks

Final command (external networking blocked, isolated temporary profile):

```powershell
.venv/Scripts/python.exe -m scripts.check_token_efficiency --label final-source-contracts tests/test_token_efficiency.py tests/test_usage_tree.py tests/test_request_snapshot.py tests/test_provider_accounting.py tests/test_context_compaction.py tests/test_compaction.py tests/test_context_learning_replay.py tests/test_context_learning_properties.py tests/test_tool_search.py tests/test_tool_result_offload_vs_truncation.py tests/test_memory_retrieval.py tests/test_memory_prompt_cache_order.py tests/test_recovery_archives.py tests/test_project_memory.py
```

**183 passed in 41.39 seconds.** Raw output and process metadata are retained
locally at `reports/token-efficiency/final-source-contracts.{txt,json}`. This includes
all 24 new test cases and the existing compaction, memory, learning, discovery,
provider-accounting and full-output recovery contracts.

Earlier checks passed 122 tests before the image change and 123 after it. A
broader run passed 173 and failed one unchanged Hypothesis input-generation
health check under load; the unmodified property suite then passed all six
tests, and the final combined run above passed. No health checks, assertions or
timeouts were suppressed. The serialization regression was observed failing
before its fix: a saved 1M-token record incorrectly priced itself as 2M after
later accumulation. It is covered by the final passing run.
Three further red tests reproduced misleading family-price inheritance for
unlisted model variants. The final implementation limits rates to catalog
names and dated aliases; tests also retain namespaced and dated known prices.
Further red controls caught mixed text/image transport overflow and unnecessary
summaries when a full repeated source moves into the tail, or the newest frame
alone is oversized. All pass in the final run, with nested-image pressure covered.

The existing expectations changed in `test_usage_tree.py` intentionally reflect
the requested behavior: unknown prices are unavailable, and raw Anthropic input
includes cached subsets exactly once. The image compaction regression now checks
separate token and transport sizes, while retaining its compaction and tool-pair
assertions.

Changing a frozen fixture fingerprint deliberately caused the replay comparator
to raise `AssertionError` and exit 1. This negative control demonstrates that
the fixture-identity check rejects that mismatch; it is not a proof that every
possible checker defect is excluded.

## Static checks

```powershell
.venv/Scripts/ruff.exe check novacode_cli/context/estimates.py novacode_cli/context/observations.py novacode_cli/context/snapshot.py novacode_cli/tracking/usage_tree.py scripts/replay_token_efficiency.py scripts/check_token_efficiency.py tests/test_token_efficiency.py tests/test_usage_tree.py
.venv/Scripts/ruff.exe format novacode_cli/context/estimates.py novacode_cli/context/observations.py novacode_cli/context/snapshot.py novacode_cli/tracking/usage_tree.py scripts/replay_token_efficiency.py scripts/check_token_efficiency.py tests/test_token_efficiency.py tests/test_usage_tree.py --check
.venv/Scripts/mypy.exe novacode_cli/context/estimates.py novacode_cli/context/observations.py novacode_cli/context/snapshot.py novacode_cli/tracking/usage_tree.py --follow-imports=silent
git diff --check
```

Ruff: **all checks passed** on these eight files; formatting: **eight files
already formatted**. Mypy: **no issues in four source files**. Raw outputs are
under `reports/token-efficiency/{lint,format,types}.txt`. Final diff review is
limited to this task's files; unrelated concurrent workspace edits are preserved.

The older `agents/context_compaction.py` has nine existing Ruff diagnostics,
compared with ten on the frozen baseline. Its existing test file also has
pre-existing lint/format issues. Changed ranges were formatted without rewriting
the rest of either file. A preliminary mypy run with imported modules skipped
reported four untyped-framework errors; the same four occurred on baseline.
The subsequent normal import-following check above passed.

## Baseline and full-suite limits

Baseline source was extracted from
`9b9cf8f1ee50bc62e7358d54ef72f9fde312e272` into the ignored
`.tmp/token-efficiency-baseline` directory, using the same installed interpreter
and dependencies. The initial selected baseline check and its isolated repeat
both produced **27 passed, 3 failed**. All three failures are in
`test_context_snapshot.py`: the two empty-checkpoint baseline cases and the
retained-tail breakdown case. They predate this implementation.

The first full-suite attempt exited after **1,010.72 seconds**, interrupted by
the configured **300-second thread timeout** around 18% completion. This timeout
method terminates pytest before its final summary. Collection order points to
`test_ctrl_b_background.py::test_bang_command_background_handoff_keeps_process_and_finishes`.
Running that test on the frozen baseline also reaches the 300-second timeout;
the repeat current-checkout run progressed past it, demonstrating load-sensitive
behavior rather than a reproducible new context regression.

The baseline diagnosis also reproduces the async prompt-expectation failure and
the small bash-output-card failure. The latter passes in the repeat current run.
The clipboard test fails in the initial full attempt but passed in the focused
baseline diagnosis; its initial failure remains load-sensitive/unattributed.
The unchanged property health check also failed in that loaded baseline diagnosis.

The two clear-context cases and replay-summary UI case passed in a baseline-only
run (**5 passed**) and in an isolated retained checkout with only this task's
implementation overlaid (**5 passed**). A larger mixed retained run encountered
three `WorkerCancelled` failures in those cases (**35 passed, 3 failed**);
baseline usage-plus-UI order passed **22**. The mixed-run failures remain
order/load-sensitive and unresolved; they are not silently classified as baseline
failures or hidden by changing the tests. These UI tests do not dispatch a model.

Full-suite commands and per-test journals are under
`reports/token-efficiency/full-final*`, `diagnosed-baseline*`, and `full-tail*`.
The repeat full run and a continuation after the first blocking Ctrl+B test
provide additional coverage; their final outcomes are recorded in the delivery
update below. No all-suite-green claim is made merely from the focused result.

### Delivery update: final source

All five implementation files in the final isolated checkout match the working
source; hashes are recorded in [source-verification.json](source-verification.json)
and the final replay report. This checkout contains baseline source plus only
this task's five implementation and three test files, excluding concurrent edits.

The final isolated full-suite command was:

```powershell
.venv/Scripts/python.exe -m scripts.check_token_efficiency --root .tmp/token-efficiency-final-source --label full-final-source
```

It exited 1 after **549.00 seconds**, at the same **300-second Ctrl+B handoff
timeout reproduced on baseline**. Before that timeout, its journal records only
four failures: the baseline async-prompt expectation and the three baseline
snapshot cases listed above. The remainder of the full suite on this exact
source is unverified. Raw journal/output/status are
`reports/token-efficiency/full-final-source*`.

During development, the wider shared-checkout repeat completed with **4,196
passed, 34 failed, 16 skipped, 1 xfailed, and 1 teardown error** in 3,585.69
pytest seconds. The continuation completed with **3,413 passed, 26 failed,
2 skipped, 807 deselected, and 1 teardown error** in 2,827.80 seconds. These runs
predate the last focused fixes and include concurrent unrelated edits; they
are additional coverage, not clean validation of the final frozen source.

Unresolved wider failures include Textual worker cancellation, subprocess startup
timeouts, loopback OAuth timing, and a config-guard teardown error (creation of
`{}` under the isolated temporary profile, not modification of the real user
profile). The journals retain every failed node and traceback. Only the failures
explicitly reproduced above are classified as baseline failures. Superseded
isolated runs were stopped after the source was finalized; they are not counted
as passing checks. No failures were hidden by changing assertions or timeouts.

## Limits

- No live provider evaluation, reasoning benchmark or real model task-success
  comparison was authorized or performed.
- Provider cache discounts, hidden SDK retry usage, high-resolution image costs
  and real output-token changes are not inferred from offline estimates.
- One of the 30 history samples changed after freezing; only 29 unchanged samples
  support the detailed history counters. Private identifiers and content remain
  outside these artifacts.
- Other source changes arrived in the shared workspace during testing. Their
  effect on unrelated UI/remote/session tests is not attributed to this change.
- No dependencies were added; configured models, context budgets, personal
  configuration, credentials and histories were preserved. Nothing was pushed.
