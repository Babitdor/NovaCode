# Context and learning acceptance contract

Implementation authorization: supplied user plan. Additional spec review not obtained (autonomous run).

Failure model: dropped constraints; shared session counters and episodes; stale instruction cache; stale automatic summaries; unreviewed global promotion; concurrent ledger writes; failed rollback; unbounded background requests.

Executable criteria live in tests/test_context_learning_audit.py and companion ledger/snapshot tests. Preserve complete user quotations across repeated compaction; isolate independent learning owners; detect current content and discovery changes; reject stale automatic summaries; persist proposals transactionally; approve only a specific revision; rollback generated records without deleting manual instructions; bound review concurrency, duration, input and output.

Use existing pytest/ruff and Python standard library SQLite; no new dependencies, paid evaluations, commits, push, or release. Full suite and configured static checks will be attempted and actual failures disclosed. Preserve public compatibility entry points where safe; ambiguous ownership must fail closed.

## Approved continuation contract (2026-10-12)

The supplied continuation plan supersedes the earlier setup restriction on test
dependencies: declare Hypothesis, coverage, and randomized-order tooling in the
development group and update the lock. Preserve all existing local work.

Failure model and falsification requirements, in phase order:

1. A single graph running different threads must resolve distinct immutable
   project/session/namespace/thread/task identities. Resume of the same thread
   preserves session ownership; clear/new thread changes it. Counters, episodes,
   calibration, request snapshots, and review queues cannot cross sessions.
2. Concurrent SQLite writers must contend on renewable thread leases. Expired or
   superseded fencing tokens cannot write checkpoints or pending writes, even
   between validation and commit. Validation and publication share a transaction.
   Memory writers serialize in process. Unsupported remote backends fail explicitly.
3. Manual compaction commits only while owning the thread and while its source
   checkpoint revision is current. Cancellation, failed archives, and stale
   preparation leave the original history and approvals recoverable.
4. Approval names one immutable proposal revision, including target, changes,
   evidence, applicability, and source hash. Approval is distinct from application.
   Activation occurs between turns through immutable artifacts and atomic manifests.
   Invalid paths/templates/variables/hashes fail without changing active artifacts.
   Rollback restores the prior manifest and preserves conflicting manual edits.
5. Candidate prompt A/B routing cannot execute an unapproved template. Legacy
   generated overrides without verifiable approval are staged; manual/approved
   overrides are retained; ambiguous ownership remains unchanged and visible.
6. Archives round-trip ordered message JSON, IDs, tool arguments/results/artifacts,
   multimodal/binary content, and approvals using checksummed immutable blobs.
   Publication precedes history replacement. Recovery inspection and restoration
   share handlers across transports; restore uses a fresh thread by default.
7. Supported adapters account for provider-prepared requests and HTTP attempts.
   Pressure and reporting share accounting; estimates/usage/unavailable remain
   distinct. Diagnostics never persist prompt content. Reviews skip wrappers whose
   retry/output caps cannot be enforced; one queued review/session, two active
   reviews/process, 60 seconds, 16k input, 4096 output, two attempts maximum.
8. Parsed memory stays within 512 entries and 16 MiB with LRU eviction. Streaming
   hashing detects restored timestamps, renames, and deletions. Discovery occurs
   off the event loop. Retrieval ordering remains stable and knowledge is data.
9. Offline Google fixtures cover stdio service selection, callback ports,
   cancellation, malformed credentials, refresh, and account isolation. No live
   account operations, paid quality evaluations, release, push, or publication.
10. Verification requires affected regressions, configured lint/types, >=90%
    changed executable-line coverage, targeted mutations, shuffled affected tests,
    and two fresh full-suite passes. Baseline failures are to be fixed without
    weakening assertions. Machine-readable evidence records actual outcomes.
    A read-only fresh-context reviewer receives contract/spec/source hash/entry
    point, with at most two rounds; material/disputed findings require user grading.

This is an append-only specification extension. Implementation progress and
unsupported behavior must be disclosed rather than represented as completed.

User clarification: keep the current approval policy. Repository-local allow
rules cannot grant permission. The obsolete policy-writer test must verify
this restriction while still checking that confirmed rules are persisted.

Additional offline test dependency: faster-whisper supplies the real audio
decoder used by existing voice-note fixtures; tests do not load an STT model.
