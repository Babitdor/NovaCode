# NovaCode context and learning audit

## Continuation status (2026-10-12; independent round 2 pending)

The implementation below has been extended substantially. Earlier evidence
and limitations in this document are historical and do not describe the
continuation's current source. Local acceptance gates pass for source hash
`e89c33b7e7bfc9f06043bf9d94b51409fd1d9fb056fa28e6877c3def8e2eae88`;
the second authorized independent review is pending.

Current implementation adds immutable per-invocation identity, session-owned
learning state, SQLite leases/fencing with atomic checkpoint publication,
coordinated async graph mutations and revision checks, manifest activation
between turns, immutable generated skills/prompts, recoverable checkpoint
archives with pending approvals, shared recovery routing, provider preparation
and HTTP attempt observation, and a bounded LRU separate from corpus discovery.
The large-corpus fix retains every discovered topic despite parsed-body eviction.
Candidate prompt routing remains disabled, and repository-local allow rules
continue to grant no tool permission, as explicitly confirmed by the user.

New fault regressions cover separate-process lease contention, expired fences,
failed SQLite commits, streaming cancellation, pinned stale revisions, crash
recovery between manifest and ledger publication, manual rollback conflicts,
binary message artifacts, manifest tampering, pending-approval resume, and
structured-output accounting. Hypothesis exercises constraint/tool pairing and
archive sequences. Offline Google stdio fixtures validate all three preset
service/port/account configurations alongside existing OAuth fixtures.

The complete local acceptance entry point `scripts/verify_context_learning.py`
reports `passed=true` in `context-learning-acceptance-final.json`, with the same
source hash before and after its run. Affected tests passed **608**, and the
same **608 passed** in global shuffled order. Two fresh full-suite processes
each passed **3,985 tests** (13 skipped, 1 xpassed); the first ran under
coverage. Changed executable-line coverage is **90.02% (1,498/1,664)**.
All seven targeted mutants were killed and restored byte-for-byte. Ruff passes
the nine new modules and the verifier script; strict mypy with imported
repository diagnostics suppressed passes the nine new modules. A separate
broader mypy run following imports reported **1,211 errors in 163 files**;
that broader check remains red and is not represented as passing.

The earlier 3,948-pass/4-failure broad run and 585-pass/80.05%-coverage
round-1 run were diagnostic source states. Their failures and the three
reviewer defects were fixed before the final local acceptance run.

Independent round 1 reviewed source hash
`7accac8a6b57f9824e82ad7d6a668407b11218ff5c1eb1bf050ce62dd0deed29`.
It received the approved contract, spec, exact state, and entry point; builder
audit/results were withheld until its blind record. Its same-hash evidence
comparison confirmed the 585 passes and 80.05% coverage, while older 538-test
evidence belonged to a different source hash. It found three material
defects: malformed generated skill frontmatter could activate, the verifier's
default verdict omitted mandatory gates, and changed-line coverage ignored
staged source changes. The user graded all three as behavioral defects and
directed fixes and round-2 verification. Regressions and implementation fixes
are now present; round 2 has not begun.

Updated offline benchmark: 32 files, 100ms cold/3.7ms discovery; 2,000 files,
4,797ms cold/955ms discovery; parsed cache 512 entries/4,956,111 resident bytes;
eight concurrent session counters each reached 100 independently. Snapshot
capture averaged 2.14ms in that sample. These are observations, not latency
guarantees or demonstrated live coding usefulness.

Migration, recovery commands, rollback behavior, and adapter restrictions are
documented in `CONTEXT_LEARNING_MIGRATION.md`. Unsupported remote persistence
cannot mutate through the coordinator; synchronous mutation methods explicitly
require the async interface. Unknown model wrappers remain approximate and
cannot dispatch auxiliary reviews unless their limits can be enforced. Old
Markdown/JSONL archives remain retained and appear as incomplete, non-restorable
references in recovery listings.
No live Google operations, paid model evaluation, release, commit, or push ran.

## Historical implementation audit

Local implementation for review; no release, commit, push, or paid live evaluation.
Baseline: `cc0fde4d066ca5ae956ad49375e0c56ba1892eee`.
The supplied plan authorized implementation. The executable spec did not receive an additional independent approval; confidence is based on local tests, not independent verification.

## Confirmed defects and changes

| Severity | Reproducible evidence | Affected behavior | Remediation |
|---|---|---|---|
| High | Four original acceptance cases failed before fixes | Shared learning counters/episodes; reset in another session erases progress | Owner-scoped namespaces, fresh task counters, serialized episode updates, explicit graph ownership |
| High | Long constraint in the middle of a user message disappeared; follow-up quotations were dropped | Automatic compaction silently loses instructions | Complete quotations carried across checkpoints; multimodal blocks retained; protected-window refusal |
| High | Review persistence invoked habit writers, skill creation and prompt evolution directly | Opt-in learning authorized broader automatic mutations than intended | Reviews prepare versioned candidates only; no automatic global promotion or evolution |
| High | Content edit with restored timestamp was not detected | Stale standing instructions; deletions retained loaded state | Content hashes and discovery identities; deleted values cleared; corpus/retrieval caches invalidated together |
| Medium | Recall followed by a successful command incremented “resolved” | Observational correlation reported as usefulness | Automatic causal credit removed; recall counts remain observational |
| Medium | Schemas cached by tool names alone | A changed tool definition could reuse its old estimate | Schema-content cache keys; assembled request snapshot used for pressure and context reporting |
| Medium | Source changes during summary preparation | Superseded automatic summary could replace the current view | Validate request generation during preparation and again before returning a checkpoint update |

Tests are in `tests/test_context_learning_audit.py`, `test_request_snapshot.py`, `test_learning_ledger.py`, `test_learning_review_safety.py`, and `test_context_learning_replay.py`. Existing fixtures were updated for intentionally changed contracts: full user quotations, session-owned store keys, current-content signatures, observational recall, required recoverable archives, and a final request observer after caller middleware.

## Execution map

| Path | Context/recovery | Learning |
|---|---|---|
| Main TUI, child tabs, headless, Telegram ingress | Shared core agent stack; steering, instructions, memory and skills precede reduction and snapshot observation | Each compiled graph names its learning owner; task counters and episodes identify their owner |
| Local subagents | Hardened middleware preserves approvals; compaction plus final snapshot | No inherited main-session learning owner |
| Packaged remote agents | Shared background compaction/snapshot helper follows model override and skill assembly | No automatic global learning mutations |
| Automatic compaction and overflow | Tool offload, argument reduction, summary/checkpoint view; original graph messages retained | Independent of learning enablement |
| Manual `/compact` and resume | Original transcript archived before rewrite; generation validation retained; full user quotations and pending tool groups preserved | Compaction is not authorization to promote generated knowledge globally |
| Memory recall and reviews | Current files and applicable ledger revisions drive cache identities | Project knowledge first, approved shared knowledge thereafter; recall does not establish causality |

`ContextSnapshot` freezes messages and tool schemas into immutable serialized tuples. Persistent diagnostics retain counts, estimates, model, generation and recovery references rather than prompt content. `/context` shows assembled-request estimates separately from observed provider usage. Counts are approximate; they are not billing-token measurements.

Reviews permit at most two active owners per process and reject overlapping work for the same owner. Preparation has a 60-second deadline, a 16,000-token estimated input cap and a 4,096-token output request cap. Supported model retry settings and cached OpenAI/Anthropic clients are bounded independently of the coding model. Empty evidence or oversized review inputs skip dispatch. Review calls use the auxiliary-dispatch purpose instrumentation. Unknown and failed outcomes cannot automatically establish successful solutions. This rollout conservatively stages all review output for approval.

The dedicated SQLite ledger serializes revisions across connections/processes, rejects stale decisions, preserves revision history and imports legacy generated lessons as unverified candidates. Markdown exports use flushed temporary files and atomic replacement. SQLite remains authoritative if export publication fails. `/learning review`, `approve <id@revision>`, `reject <id@revision>`, and `rollback <id@revision>` share handlers. Existing on/off/status settings remain supported and disabled by default. Approved generated knowledge is retrieved as data; skill/prompt proposals do not rewrite installed skills or active prompt templates.

## Verification evidence

- Initial RED: all four original regression cases failed on the original implementation.
- Final combined runner: **485 tests passed**, including 20 MCP preset checks; new-module lint passed. `context-learning-results.json` records the source hash, exact commands, timings and model-free accounting sample.
- Four manual mutants were detected: dropped steering, shared counter ownership, stale revision approval, and stale content identity. Every target file was restored byte-for-byte. `context-learning-mutations.json` records actual failing tests. The runner also failed closed when an initial mutation target did not match Windows line endings; this is a limited negative control, not proof that every checker is sound.
- Configured mypy checked snapshot and ledger modules successfully. Existing modules retain pre-existing lint debt; new-module lint is a strict gate.
- Complete broad run: **3,859 passed, 48 failed, 15 skipped, 1 xpassed**. Of those 48 failures, **37 also failed on untouched HEAD** in the baseline replay; **11 passed** there. Contract mismatches in compaction/subagent tests and the persistence callback seam were corrected and reverified in focused runs. Other cases passed in isolation and remain suite-order/environment concerns rather than established product defects.
- A later fresh full run with a 20-second test deadline terminated in a headless HTTP test. It is not a green full-suite result. Preserve the XML/output artifacts; do not interpret focused passes as a clean release gate.
- Paired scripted-model replay with learning off/on preserved constraints, valid tool pairing, the same final task result, and no unreviewed activation. This demonstrates mechanical invariants, **not improved live coding quality**.
- Snapshot timing is an observational local sample, recorded in JSON. It varied under concurrent suite load; no latency SLA or live quality claim is made.

Reproduce:

```powershell
.venv/Scripts/python.exe scripts/verify_context_learning.py
.venv/Scripts/python.exe scripts/verify_context_learning.py --mutations --output context-learning-mutations.json
.venv/Scripts/python.exe scripts/verify_context_learning.py --full --output context-learning-full-results.json
.venv/Scripts/mypy.exe novacode_cli/context/snapshot.py novacode_cli/hermes/ledger.py --follow-imports=silent --ignore-missing-imports
```

Run mutations only when other tests are idle. Baseline reproduction uses `scripts/reproduce_context_learning_baseline.py` with an untouched `git archive HEAD` tree and the broad-run XML. Hypothesis 6.169.0 and sortedcontainers 2.4.0 were installed into the existing virtual environment because an existing test could not collect; no runtime dependency was added.

## Suspected risks and measured limits

These are deliberately not presented as fixed or as proved failures:

- Automatic generation checks observe the request and its summarization event. They do not provide a distributed compare-and-swap across unrelated processes mutating the same graph checkpoint. Transport/session locking must still serialize such writers. Manual generation validation likewise cannot substitute for a distributed mutation protocol.
- Snapshots observe Nova's final `ModelRequest`. Provider adapters can further transform multimodal content and structured-output schemas. Exact wire-byte/token accounting requires adapter-specific instrumentation; estimates and unknown usage are labelled accordingly.
- Pending tool groups are protected from summarization/argument reduction. Original checkpoints retain overflow-clipped results, but not every legacy transport exposes identical recovery browsing controls.
- Legacy migration recognizes the generated lesson header during `/learning review`. Original files and manually pinned files are retained. Unrecognized legacy files are not automatically reclassified, and previously approved user overrides remain untouched.
- Approved skill/prompt proposals are recoverable knowledge records, not automatic installations or replacements of active templates. Global mutation application needs a separate reviewed action.
- Parsed memory corpus caching is bounded to 512 entries. Current-content discovery still reads file identities; large directories can impose I/O overhead. No new semantic-ranking experiment was enabled or coding-quality benefit asserted.
- Generic third-party model wrappers may hide their own HTTP retries or output-limit semantics. Only the visible supported SDK settings are controlled here; content-free HTTP observations remain the source of truth for actual attempts.
- Learning ownership isolates independently built graphs. Sharing one compiled graph across multiple concurrent checkpoint threads still requires transport-level ownership/isolation; this rollout does not establish that stronger guarantee.
- Existing test-order contamination and dependency mismatches prevent claiming zero new failures across the complete suite. Independent verification, enforced changed-line coverage and property-based generation were not completed; the evidence is narrower than the entire original rollout matrix.

## Migration and rollback

Use `/learning review` to inspect generated legacy lessons and proposals. Review shows scope, evidence, intended content and revision history. Approve the exact displayed revision; a changed candidate requires another approval. Use `/learning rollback <current-id@revision>` to deactivate generated knowledge while retaining its history. Rejected/rolled-back knowledge stays in SQLite for inspection. Originals and manually authored instructions are never deleted by this flow.

For code rollback, restore the changed implementation files from the baseline only after saving the local diff. Retain `learning.sqlite3`, exports, original Markdown and conversation archives for recovery. Turning learning off stops automatic review preparation but does not delete records. No hosting, publishing, release or push was performed.

## Comparison sources

[Agent Harness Anatomy](https://dpsm.github.io/agent-harness-anatomy/) informed the comparison map. [pi's pinned compaction implementation](https://github.com/earendil-works/pi/blob/cd32f7725fdbddbaecdff5b1e68491563394e0ca/packages/coding-agent/src/core/compaction/compaction.ts) informed recoverable boundaries and policy/mechanics separation. [Hermes' pinned background review](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/agent/background_review.py) informed the distinction between preparation and curation. Nova's graph framework, approvals and transports were retained; these projects were not substituted into Nova.

## Follow-up: Google MCP presets

Added `google-drive`, `gmail`, and `google-docs` to the existing shared catalog. They use `uvx workspace-mcp` in stdio mode with service-specific core tools, one OAuth client JSON path prompt, and distinct loopback callback ports (8001, 8002, 8003). Source flags and OAuth-file support were verified against the [upstream server source](https://github.com/taylorwilsdon/google_workspace_mcp/blob/main/main.py) and [OAuth configuration](https://github.com/taylorwilsdon/google_workspace_mcp/blob/main/auth/oauth_config.py). All 20 preset tests pass, including template immutability and existing presets. Preset/test lint passes. README documents setup. No Google account was connected and no mail/document/Drive operation was executed; authenticated end-to-end checks were not performed.
