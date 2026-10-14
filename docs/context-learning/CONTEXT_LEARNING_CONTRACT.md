# Approved task contract

Implement the supplied plan locally, preserving existing changes, framework,
approvals, and transports. No paid model evaluations, Google account operations,
release, push, or publication are included. Authenticated Google validation and
live coding-quality evaluation require separate authorization.

Approved defaults: apply approved changes between turns; require review for
unverifiable generated overrides; fix existing full-suite failures; use offline
evaluations and OAuth fixtures; perform independent verification.

## Session safety

Introduce immutable project/session/checkpoint-namespace/thread/task identity,
independent of the compiled graph. Each session owns counters, episodes, review
queue, calibration, and request state. Ownership survives resume; clear creates
a new identity. Closure releases runtime resources.

Coordinate all streaming, resume, compaction, imports, restore, and direct state
updates. SQLite uses renewable per-thread leases and fencing, validated in the
same transaction as checkpoint publication. Memory uses process-local locking.
Release leases when streams finish or pause; preserve checkpoint approvals.
Compaction commits against its captured source revision under valid ownership;
discard failed, cancelled, stale, or superseded preparation. Remote backends
must provide equivalent coordination before concurrent writing; explicitly
report unsupported restrictions.

Reviews use immutable episode snapshots, at most one queued review per session,
two active reviews per process, existing deadline/token caps, and two provider
attempts maximum.

## Reviewed activation

Replace the LearningProposal alias with a distinct versioned proposal containing
target, intended changes, evidence, source-content hash, and applicability.
Approval and application are separate. `/learning approve <id@revision>`
authorizes exactly the displayed revision; activate at the next turn boundary.
Current turns keep their captured configuration.

Install generated skills/prompts with immutable versioned artifacts and atomic
activation manifests. Validate paths, syntax, required variables, and target
hashes. Project artifacts stay project-scoped. Shared skills, habits, and prompts
always require approval. Project lessons may activate automatically only with
referenced explicit confirmation or relevant successful verification supporting
the specific claim.

Route evolution, refinement, prompt commands, and outcome promotion through this
lifecycle. Disable unapproved template rendering and candidate A/B routing.
Recoverably import recognized generated files: preserve manual or explicitly
approved overrides, stage generated overrides lacking verifiable approval, and
leave ambiguous files untouched while flagging review. Rollback restores the
prior manifest; preserve manual edits and report conflicts. Shared command
handlers expose pending/applied/failed/rejected/rolled-back states.

## Recovery and accounting

Unify manual/automatic archives with versioned message JSON, checksummed
manifests, and immutable blobs for binary content/offloaded results. Preserve
IDs, tool arguments/results/artifacts, ordering, and approvals. Publish before
destructive compaction; archive failure prevents replacement. Retain old
archives and expose incomplete legacy references clearly.

Shared `/context recovery` listing/inspection/restore handlers must serve
console, TUI, headless, and Telegram. Restore into a new thread by default.
Capture supported provider-prepared requests, structured schemas, multimodal
transformations, effective output reservation, and actual HTTP attempts. Use
one accounting source for pressure and `/context`; distinguish estimates,
reported usage, and unavailable data. Diagnostics are content-free. Unsupported
wrappers stay explicitly approximate; skip reviews whose retry/budget limits
cannot be enforced.

## Performance and offline Google validation

Bound parsed-memory caches by both 512 entries and 16 MiB with LRU eviction.
Stream hashing; move discovery off the event loop. Preserve detection of
restored timestamps/deletions/renames and retrieval ordering. Keep new ranking
experiments disabled; retrieved knowledge never grants authorization.

Benchmark small/large corpora, concurrent sessions, review counts, cache growth,
context overhead, and latency. Report observational recall separately from
demonstrated usefulness. Test Drive/Gmail/Docs presets using fake stdio servers
and local OAuth fixtures for callback ports, service selection, cancellation,
malformed credentials, refresh, and account isolation.

## Verification and delivery

Update CONTEXT_LEARNING_SPEC.md with falsifiable requirements before coding.
Regress shared graphs, cross-process writers, lease loss, stale commits,
activation crashes, rollback conflicts, multimodal recovery, and approvals.
Add property-based compaction/tool-pairing tests and fault injection for failed
writes/migrations; declare test dependencies and update the lock.

Fix reproduced full-suite failures, including baseline failures, without weaker
assertions or blanket skips. Remove ordering contamination; reliably close
subprocesses, servers, and stores. Require affected regressions, configured
lint/types, at least 90% changed executable-line coverage, targeted mutations,
two clean full-suite runs in fresh processes, and shuffled affected tests.

Authorize one separate read-only verifier for up to two independent rounds.
Before builder evidence, give it this contract, the spec, exact source hash,
and verification entry point. Material/disputed findings require user grading.
Deliver machine-readable results, updated audit, migration/recovery guidance,
rollback commands, and explicit unsupported/external-validation limits.

## Explicit user clarification

Keep the current approval policy. Update the obsolete baseline test to verify
repository-local allow rules cannot grant permission to `npm run build --prod`.
