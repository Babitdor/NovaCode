# Token efficiency and context reliability — 2026-10-17

The fixed offline replay reduces cumulative estimated input from **251,140 to
123,094 tokens (50.99%)**, including the replay's auxiliary inputs. No individual
case increases. All three sequential repetitions give identical token counts.
This is an observation-heavy synthetic corpus, not an estimate of typical session
savings, a provider bill, or evidence of better model reasoning/task success.

## Measurement boundary

- Baseline: `9b9cf8f1ee50bc62e7358d54ef72f9fde312e272`, with the existing `.gitignore`
  edit preserved. Additional edits arriving in the shared workspace were left alone.
- Dependencies unchanged: langchain-core 1.6.6, langchain 1.3.18, langgraph 1.2.11,
  deepagents 0.7.10. The final raw report also fingerprints the dependency files
  and changed implementation files. No new packages or paid calls.
- `scripts/replay_token_efficiency.py` freezes synthetic messages and schemas by
  SHA-256, dispatches growing request prefixes through the production snapshot
  middleware, and records each final assembled request. Models are never invoked;
  socket connections fail closed and each repetition has an isolated profile.
- Each case counts its main requests plus two fixed synthetic auxiliary inputs
  representing verification and learning. These auxiliary inputs are estimates,
  not observations of actual autonomous verification/review behavior. They retain
  the same tool roster before and after. No auxiliary calls are hidden or removed.
- Both measurements use LangChain's approximate character/message/schema counter.
  Image tokens use a fixed 85-token heuristic, independent of base64 length;
  dimensions, actual image tokenization, output tokens, provider usage and hidden
  retry usage remain unavailable. Transport size is recorded separately.
- Output tokens are `null`, not zero. The replay has zero compactions; real
  compaction/restart contracts are exercised separately by the focused tests.
  Rereads and call counts are unchanged. Request-assembly latency is recorded for
  every repetition, but does not measure model or end-to-end latency.

Whole-corpus assembly durations were 852.71 / 38.41 / 38.31 ms at baseline and
240.91 / 22.78 / 21.69 ms for the final retained measurement (median 38.41 vs
22.78 ms). An earlier loaded repetition had a 608.42 ms median. Cold imports
and substantial concurrent host load confound this
comparison. The token reduction does **not** establish a latency improvement.
The raw `rereads` field is a legacy label counting all file-read results;
the final report adds correctly named `read_calls` and `reread_calls` fields.
Each repetition has 56 main requests, 24 auxiliary inputs, 49 completed reads
and 39 repeated exact-argument reads. All counts are unchanged. Auxiliary
input stays at 22,712 tokens; main input falls from 228,428 to 100,382.

## Paired input estimates

| Replay | Baseline | Retained | Reduction |
|---|---:|---:|---:|
| Short edit | 234 | 234 | 0% |
| Multiple files | 22,841 | 13,141 | 42.5% |
| Long conversation with correction | 22,892 | 8,342 | 63.6% |
| Large test output | 9,980 | 9,980 | 0% |
| Reads after edits | 22,853 | 11,207 | 51.0% |
| Large tool catalog | 31,957 | 31,957 | 0% |
| Memory instruction | 29,302 | 9,902 | 66.2% |
| Approved-plan handoff | 29,302 | 9,902 | 66.2% |
| Image | 287 | 287 | 0% |
| Cancellation / pending call | 29,314 | 9,914 | 66.2% |
| Background-task continuation | 29,337 | 9,937 | 66.1% |
| Separate session input | 22,841 | 8,291 | 63.7% |
| **Total** | **251,140** | **123,094** | **50.99%** |

The memory/plan/background replay labels test retention of the relevant input
messages; they do not simulate the whole corresponding subsystem. Production
memory retrieval, tool discovery, archives, learning and repeated compaction have
separate regression coverage. Budget/concurrency tests exercise 4k, 128k and 1M
windows, output reservations, protected overflow, two concurrent sessions, and
restart with and without the earlier source observation.
These are assembled-input replays, not complete agent-task execution traces.

Raw data: [baseline.json](baseline.json), [observations.json](observations.json)
(first retained candidate), and [final.json](final.json). The last candidate changes
pressure/accounting correctness, not the replay's estimated token reduction.

## Read-only history audit

The initial manifest selects the 30 most recently modified `recent.jsonl` files
and fingerprints their corresponding archives. Session identifiers and hashes
remain only in `.tmp/token-efficiency-history-manifest.json`. Histories,
configuration and credentials are never modified or copied into this directory.

One selected session changed after freezing. Its original bytes were not
retained, so detailed analysis marks that session unavailable rather than
resampling. The other **29 unchanged sessions** contain **1,045 tool results,
286 duplicate result payloads, 375 reads, and 155 repeated read arguments**.
Their serialized content totals 3,964,577 characters; simple text-marker counts
find 11 correction/constraint messages and 11 recovery markers. Those markers
are descriptive heuristics, not semantic judgments about recovery success.
Repeated reads may be necessary after edits. Duplicate outputs from arbitrary
tools are not evidence that the tools themselves can safely be skipped.

## Pipeline audit and ranked decisions

| Rank / stage | Evidence and contribution | Risk and retained decision |
|---|---|---|
| 1. Tool observations | Repeated file reads dominate this replay; the local sample independently shows rereads and duplicate output. | Replace only subsequent successful `read_file` text with identical arguments and bytes. Keep one complete source in the same request and every original checkpoint message. Every tool still executes. Medium semantic risk remains until live evaluation. |
| 2. Prompt/tool schemas | `core_agent.py` assembles middleware; `tool_search.py` already binds core, frequent, discovered and called tools. The large-catalog replay alone costs 31,957 input tokens including auxiliary inputs and does not improve. | Retain discovery, explicit skill tools and stable inventory notes. Further catalog trimming was considered but not implemented: extra discovery turns or inaccessible tools could outweigh savings. |
| 3. Compaction/recovery | `context_compaction.py` builds an effective view; `compaction.py` handles summarization. Full messages and binary artifacts are recoverable through `context/archives.py`. | Count the same duplicate-observation view when deciding pressure. Preserve original summary input/history, pending calls, user corrections and recovery files. Do not lower context budgets or remove restoration/verification. |
| 4. Memory/skills | `memory/agent_memory.py` already separates the stable memory/base prompt from volatile retrieval after the cache breakpoint. Refreshing skill middleware persists pinned state and resolves activated tools. | Preserve explicit/project instructions, current retrieval limits and access to full sources. No evidence supports deleting blocks or deduplicating instructions across precedence boundaries. |
| 5. Auxiliary work | Usage scopes and local metrics already attribute main, verification, goal, compaction and learning requests; model construction attaches the shared callback. | Do not disable review, retries, verification, polling or completion safeguards merely to save tokens. Actual auxiliary savings were not demonstrated; synthetic auxiliary inputs remain fully counted. |
| 6. Multimodal pressure | The old `_request_size_tokens` adds base64 chars/4 to image tokens. A regression reproduces 21 vs 6,020 estimated tokens for equivalent image blocks differing only in encoded length. | Retain a separate transport guard, while estimating image tokens independently. Handle nested image blocks without charging base64 as text. Fixed image estimates remain approximate and can undercount real high-resolution input. |
| 7. Usage/cost attribution | `/usage` totals call `estimate_cost` without a model; mixed-model scopes overwrite their earlier model label. | Store additive per-model input/output totals and sum costs across descendants. Unknown prices and missing usage render `unknown`. Old records load without migration using their recorded model; previously lost mixed-model attribution cannot be reconstructed. |

The main and auxiliary entry paths share model construction and context
middleware: `core/agent_loop.py`, headless/worker/remote consumers, and packaged
background agents. The final snapshot records text/schema/image estimates,
purpose, model, provider-reported input when available, HTTP attempts when
observable, and explicit unavailable retry usage. Existing usage-tree and local
metrics counters remain authoritative; estimates are not added to reported usage.

## Retained implementation

`context/observations.py` makes a request-local copy. It matches the call and
result in order, then keys successful text reads by exact serialized arguments
and text. Only results of at least 2,048 characters qualify. Different file
arguments, changed output, errors, images and side-effecting tools remain whole.
The reference names an earlier full result in that request; references are
rebuilt from originals after every compaction/restart. Persistent histories,
call/result pairing, instruction order and fresh file execution stay intact.

`ContextSnapshotMiddleware` applies that view immediately before dispatch in
both sync and async paths. Compaction pressure uses the same view but archives
and summarizes originals. Summary worthiness compares the actual dispatched
views, so moving a repeated full source into the tail is not counted as savings.
It also avoids summaries that cannot meaningfully shrink an oversized latest
frame. `context/estimates.py` separates text and image estimates. Compaction
keeps its combined text/image-body guard and tail pairing independently of
token estimates, including nested image blocks.

`tracking/usage_tree.py` persists per-model totals, propagates unknown costs and
counts calls with missing usage as unavailable. Raw Anthropic input adds its
cache subsets once; normalized LangChain/OpenAI input is already inclusive and
is not charged again. Cost values remain estimates from the existing bundled
list-price table, without provider discounts or cache-specific pricing. Only
catalog names and dated snapshot aliases receive a rate (provider namespaces
are accepted); unlisted variants do not inherit a guessed family price.

Remaining accounting limits include calls outside an active usage scope and
unobserved retry usage. Existing per-turn persistence keys also use whole-second
timestamps: two persisted turns in the same thread starting within one second
can overwrite one another. That storage-key issue is not addressed by this
request-view optimization; the new per-model aggregation fixes costs for the
usage records that are present.

## Verification and limits

See [VERIFICATION.md](VERIFICATION.md) for commands, test outcomes, baseline
comparisons and full-suite limits. No live evaluation was authorized or run.
Scripted/replayed success means a context contract passed; it does not establish
successful real model task completion.

## Reproduce

```powershell
.venv/Scripts/python.exe -m scripts.replay_token_efficiency --output reports/token-efficiency/replay.json --baseline docs/context-learning/token-efficiency/baseline.json
.venv/Scripts/python.exe -m scripts.check_token_efficiency --label focused tests/test_token_efficiency.py tests/test_usage_tree.py tests/test_request_snapshot.py tests/test_provider_accounting.py tests/test_context_compaction.py tests/test_compaction.py tests/test_context_learning_replay.py tests/test_context_learning_properties.py tests/test_tool_search.py tests/test_tool_result_offload_vs_truncation.py tests/test_memory_retrieval.py tests/test_memory_prompt_cache_order.py tests/test_recovery_archives.py tests/test_project_memory.py
.venv/Scripts/python.exe -m scripts.check_token_efficiency --label full
```

The replay exits nonzero if fixture/call boundaries differ, a context assertion
fails, aggregate reduction falls below 10%, or any replay grows more than 5%.
The test runner strips credential environment variables, isolates the profile,
blocks external socket connections, and persists output, per-test progress and
exit status under `reports/token-efficiency/`. Loopback is allowed for local
test fixtures. The optional `--histories` audit reads the frozen local sample;
it is not needed to reproduce synthetic measurements.
