# GitHub issue review — 12 November 2026

Scope: the six open issues in `Babitdor/NovaCode`, #4 through #9, read from
GitHub's issues API. They had no comments at review time. Work is local; no
release, push, issue closure or publication was performed. Command Center was
initially deferred, then resumed when the user supplied the reference at
`assets/screenshot/CommandCenter.png`.

## #9 — Startup stalls

Confirmed source defects: settings initialization synchronously discovers the
OS keychain before the UI loads, and credential reads had no deadline. A stalled
keychain could therefore hold startup indefinitely. Separately, fallback-file
reads ignored failure to acquire their lock and released a lock they did not own.

`config/keychain.py` bounds backend discovery and password reads, serializes
access, and uses a daemon worker so an abandoned read cannot prevent shutdown.
Only one operation can remain outstanding. Late results cannot update the
environment or credentials. Writes retain synchronous completion semantics.
Settings retain environment credentials when backend discovery times out; no
timeout silently creates a fallback credential file. Failed file-lock acquisition
now returns without reading the file or releasing another owner's lock.

`startup.log` now records `heavy_imports_start` and `heavy_imports_ready`, before
the existing session phases. Log-directory errors cannot abort startup. A cold
launch after an actual system reboot remains to be verified: the issue did not
provide a process stack, so keychain blocking is a reproduced failure mechanism,
not proof that it was the only cause of the original report.

## #8 — Relevant memory recall

The reported workspace fix was absent in this checkout. Retrieval now requires
semantic similarity of at least 0.45 or strong lexical evidence (score >= 3)
when a complete semantic score vector is available. A lone title token scores
2 and no longer overrides low similarity. Exact topic names remain searchable.
Without embeddings, including an incomplete score vector, the existing lexical
floor of 2 is retained and ranked explicitly by lexical relevance. A single body
token still fails the gate. This preserves usable offline and short-query recall.

Provider-specific reranking exceptions now fall back to the already filtered
local matches, as invalid JSON rankings and transport errors already did.
Cancellation still propagates. Reranking cannot invent or add candidates.

Regression cases cover weak overlap, strong overlap, exact names, paraphrases,
missing/incomplete embeddings, lexical fallback ordering, invalid rankings,
provider exceptions and model-unavailable behavior. The thresholds are tested
contracts, not a live-model precision/recall benchmark. Broad queries may still
match genuinely similar memories; no unsupported accuracy percentage is claimed.

## #7 — LangSmith migration

The [official migration guide](https://docs.langchain.com/langsmith/smithdb-sdk-migration)
sets Python's minimum at 0.10.15 and removes the old Cloud endpoints on
31 January 2027. Both projects now require and lock 0.14.17, the stable version
resolved during this review. The evaluation lock also refreshes its stale local
Nova package metadata and dependencies to match the current root package.

| Location | Usage | Decision |
| --- | --- | --- |
| `nova-evaluation/scripts/harbor_langsmith.py`, `_process_trial` | Python SDK `Client.list_runs(project_name=..., filter=..., is_root=True)` | Replaced by `tracking/run_queries.py`: resolve project UUID, query `client.runs.query`, select IDs, consume cursor pages asynchronously. |
| `novacode_cli/tracking/tracing.py` | Client construction, trace contexts, trace wrappers and uploads | No deprecated query endpoint used. |
| `nova-evaluation/scripts/harbor_langsmith.py` | Dataset/experiment operations and feedback APIs | Outside the reported run-query migration; raw HTTP calls preserved. |
| `tests/test_agent_stream.py` and tracing tests | Offline context/client fixtures | Revalidated against the upgraded SDK. |
| `prime.py` and local TUI run listings | Local filesystem `_list_runs` helper | Unrelated to the LangSmith SDK. |

The source search covered application, scripts, evaluation code, tests, docs,
examples, plugins and CI, including method references and endpoint strings.
No other source call to a deprecated run query was found. SDK-internal legacy
implementations are dependencies, not executable call sites owned by this repo.

The replacement preserves all historical traces with an explicit epoch lower
bound; the new API otherwise defaults to one day. It uses only IDs, so changed
token/cost aggregation and renamed response fields do not affect this caller.
Two results suffice to reject ambiguity. Trial names are JSON-escaped in filters.
The feedback CLI now keeps one event loop across trials for the SDK's async
transport. Tests intercept the installed SDK's HTTP transport and verify the
actual v2 endpoint, payload and cursor pagination without sending requests.

Migration feedback: the guide's async iteration/project UUID examples apply to
this caller. Its time-window warning is essential to avoid silently losing older
trials. No undocumented parameter substitution or raw-HTTP-to-SDK conversion was
needed. Live Cloud and self-hosted behavior remain unverified; self-hosted needs
server 0.16 or newer for these methods.

## #5 — Code Mode reliability

No production logs or per-error counters were supplied, so failure frequency
cannot be established from this issue. Offline reproductions distinguish actual
reporting defects from intentional guards:

| Failure class | Evidence / disposition | Recovery |
| --- | --- | --- |
| Syntax or Markdown quoting | Existing syntax/quoting graph tests; intentional compiler rejection | Fix the snippet; discovery and direct text writes remain available. |
| Wrong name, schema, path or failed edit | Native/MCP validation and filesystem-result tests | Discover names and schemas; inspect the failed operation before retrying. |
| Approval denied, stale conversation, missing human handler | Shared-loop approval/cancellation tests; intentional fail-closed behavior | Supply a valid current approval through the owning conversation. |
| CPU, wall or nested-call timeout | Existing deadline/cancellation tests; missing actionable hints reproduced and fixed, including QuickJS's separate native timeout exception | Error responses now recommend smaller batches and inspecting completed calls. Side effects are not rolled back. |
| Call, heap, argument or output budget | Existing boundary tests; guards retained | Reduce the operation or inspect smaller slices of saved values. |
| State restoration followed by a failed snippet | New regression reproduced missing `state_warning`; fixed | Both success and error envelopes now disclose cleared variables. |
| Host/MCP failures or partial side effects | Existing error and cancellation tests | Keep completed-call evidence; do not replay writes blindly. |

No production Code Mode budget was raised. Interpreter isolation, direct
control tools, permission checks and nontransactional side-effect behavior remain
part of the current contract.

Command Center is now implemented as `/command-center` (alias `/cc`), following
the supplied reference's project grouping, status filters and task details.
It merges saved conversations with live panes, keeps browsing off the transcript,
loads previews off-thread, and uses the existing session-tab/resume safeguards.
See [Command Center](command-center.md) for navigation, status semantics and
the distinction between recorded history and live observations. No new runtime
dependency is required.
The verification script's outer subprocess deadline increased from three to ten
minutes after the passing focused suite took 208 seconds without coverage. This
changes verification orchestration only; interpreter and individual test limits
remain in force.
Coverage also reproduced a native QuickJS timeout before the approval test's
first host call: the test allowed only 50 ms of CPU time for cold setup. That
fixture now allows 250 ms CPU and two active seconds, while its human wait lasts
2.2 seconds and still exceeds both budgets. Exact side-effect and approval
assertions are unchanged; captured tool errors now explain any future failure.

## #4 — Learning, dream and wiki review

Reviewed the revision ledger, turn-boundary activation, review handlers, explicit
memory search, skill proposal routing and wiki/dream tests. Approval still names
an immutable revision; application remains a separate turn-boundary operation.
Project ownership, content hashes, rollback conflicts, proposal-only dream writes,
wiki path confinement and prompt-injection boundaries have offline contract tests.
Standing `agent.md`, habits and a capped navigation index are intentional prompt
context; topic bodies remain explicitly retrieved rather than injected wholesale.
The keyed durable-store tools are explicit key lookups, not semantic recall.

Found and corrected inaccurate CLI/TUI messages claiming automatic approval of
wiki lessons. Current review code deliberately keeps those lessons pending even
with automatic skill approval enabled. Updated the stale test to preserve that
boundary. Another old test looked for generated skills in an obsolete local
ledger; its fixture now binds the authoritative proposal ledger explicitly.
No approval boundary was weakened to make those tests pass.

The older context-learning contract's statement that shared skills always need
manual approval differs from the current explicit `/learning auto on` option.
Current behavior permits automatic skill-proposal approval only with that user
setting; wiki lessons, habits and prompt changes remain review-gated. Treat the
old contract as historical scope where it differs from these current commands.

This is an offline source/contract review. It does not establish live learning
quality or certify that every learned assertion is useful. Corpus-size and
model-quality evaluation remain separate from state-machine correctness.

## #6 — Prime factory readiness

Verdict: implementation is present, but unconditional production readiness is
not established. The four stages, durable controller/storage, exact-plan and
candidate publication gates, isolated worktrees, test-first evidence, bounded
repairs and continuation/reset fencing are represented in source and offline
contract tests. Approval of a plan does not grant permission to publish.

The factory tests exercise real temporary Git repositories and scripted workers,
with fake providers/publication. The multi-repair workflow exceeded its existing
60-second deadline here. Stack diagnostics showed Git subprocess work, including
integration queries, so review uses explicitly bounded extended test deadlines;
passing with that configuration must not be described as passing the default run.

Required conditions before a production claim:

1. Complete Linux and supported-Windows validation, including the broad suite;
   resolve reproducible failures and distinguish machine-dependent test timing.
2. Exercise a live provider and version-matched documentation retrieval in a
   disposable repository, recording actual token, latency and cost evidence.
3. Exercise the real Docker containment profile and cancellation/recovery paths.
4. In a separately authorized disposable destination, validate publication and
   recovery across push/PR uncertain outcomes. Offline mocks cannot prove this.
5. Accept or remediate loss of active-time accounting since the last durable
   checkpoint after abrupt host death. Existing token reservations are a separate
   mechanism and do not eliminate this time-accounting gap.

The repository-root/committed-baseline requirement and Windows OS-sandbox fallback
are explicit limitations. The absence of a dedicated GitHub issue picker does
not prevent explicit-goal operation. Historical benchmark counts in
`FACTORY_PROGRESS.md` remain historical and are not current certification.

## Verification

Fresh Windows / Python 3.11.14 checks:

| Selection | Result |
| --- | --- |
| Complete offline factory suite | 109 passed with explicit 180-second test deadlines; 476.58 seconds total. The default run timed out. |
| Complete Code Mode tests plus memory reranking regressions | 139 passed; 208.44 seconds. |
| Final randomized Code Mode suite under branch coverage | 127 passed; 299.44 seconds, 91% total coverage across Code Mode, runtime and tool-result helpers. |
| Four Code Mode negative controls | All four produced the expected assertion failures when argument validation, call budgets, approval evaluation or control-tool exclusion was disabled. |
| Startup, credentials-file ownership, memory search and startup/search performance | 43 passed. The final extra nonfinite semantic-score regression is also included in the 139-test selection above. |
| Learning ledger/recall/commands/TUI, keychain and actual SDK transport follow-up | 29 passed. |
| Async-agent routing/retry, clipboard and adjacent compaction follow-up | 37 passed. |
| Tracing, agent streaming and memory compatibility after SDK upgrade | 66 passed before the final extra memory cases. |
| Mypy on keychain, run query, memory search and Code Mode modules | Passed, four source files, `--follow-imports=silent`. |
| Ruff on changed runtime helpers/new tests and focused import checks | Passed. Existing unrelated lint debt in `main.py` and older test files remains. |
| Repository-wide test run | Did not finish: two assertion failures before a Textual timeout, roughly 936 completed of 7,908 collected tests. |

The broad-run async-agent assertion incorrectly expected a separate retry
middleware; model routing already invokes bounded subagent retry. Its expectation
was corrected to check the routing middleware, preserving approval/shell controls.
The clipboard and adjacent compaction checks passed in isolation; suite-order or
timing sensitivity remains unverified. The broad suite must not be reported green.

The first Code Mode verification-script run exceeded its old 180-second outer
deadline. A subsequent covered run exposed the 50 ms cold-start allowance in the
approval test, which is corrected as described above. The final randomized run
passed all 127 cases and the 85% coverage gate, all verifier lint/format checks,
and Mypy on six source modules. Its compatibility selection then hit an individual
pytest timeout after approximately 53 completed cases; the full verifier therefore
did not pass. The four original negative controls were run separately and all
failed the intended assertions, with no collection errors. Their reproduction
helper and full output are `.tmp/run_code_negative_controls.py` and
`.tmp/code-negative-controls.log`. That helper skips repeating baseline/static
commands already run; it does not change the four verifier mutations.
Startup-log tests also isolate and close their logger so early `main` imports
during full-suite collection cannot redirect the test's expected log file.
Factory extended-deadline reproduction is retained locally in
`.tmp/run_factory_review.py`; it overrides only pytest deadlines, not assertions
or application limits. XML/log artifacts for these selections are under `.tmp/`.
No new direct runtime dependency is introduced. LangSmith's minimum and both
lockfiles change as described above; live reboot/provider/Docker/publication and
Linux validation are outside the completed offline evidence.

## Command Center follow-up

After the reference screenshot became available, the Command Center portion of
issue #5 was implemented as `/command-center` (`/cc`). It includes project/status
grouping, filters, search, selected-session details, paginated saved sessions and
cached live-tab updates. Opening a saved session uses the existing tab-resume
flow. See [Command Center](command-center.md) for controls and data limitations.

The final focused selection passed **43 tests** across the Command Center,
command help, tab resume and session launch. Ruff format/lint, Mypy on the new
screen and `git diff --check` passed. Wide and narrow renders were visually
reviewed using fixture session data; responsive tests also cover 60-column
terminals. The full repository suite was not rerun for this follow-up, and the
broader validation limitations above still apply. No runtime dependency was added.
