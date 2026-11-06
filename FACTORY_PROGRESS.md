# Prime software factory implementation

Existing edits to team recovery, CI, FEATURES and attachments are prerequisites/user work.
Do not revert them. The initial implementation scope excluded release, push, merge
and deployment. The user subsequently authorized pushing to main; the coordinator
recovery prerequisite is committed separately, and unrelated edits remain local.

1. Durable host controller, budgets and legacy compatibility. Check: focused controller tests.
2. Versioned local research/knowledge and frozen plan gates. Check: offline provenance,
   invalidation and approval tests.
3. Isolated development, observed checks, bounded repair and publication preparation.
   Check: temporary Git repositories and scripted workers, including restart/failure paths.
4. Shared entry point, role settings, dashboard and mandatory human controls.
   Check: interface/compatibility tests, lint, offline suite and independent review.

Plan critique: existing Prime is checkpoint guidance, not a scheduler. The new controller
must intercept the shared loop before model execution, use worker-bound RPC rather than
model-authored execution evidence, and keep legacy records readable. Approval consumers
must reject mandatory factory gates without a human handler even under auto approval.
No graph/parser dependencies or deepagents upgrade are needed. Provider/network/PR live
checks require available authorization; offline tests must not publish anything.

Status: all four implementation stages are present; final focused offline verification
passed. Remaining limitations are recorded below.

Implemented behavior:

- Durable controller and worker identities, shared reservations and usage, explicit
  pause/resume, mandatory plan and candidate-specific publication approvals.
- Version-aware fetched documentation with provenance, bounded local AST knowledge,
  frozen acceptance definitions, isolated integration/task worktrees and file leases.
- Test-first observations, independent verification/security, bounded repairs, specific
  human waivers, publication intent and uncertain-outcome reconciliation.
- Shared agent-loop entry, legacy report inspection, separate role settings, compact
  dashboard and TUI/headless/remote approval routing. No new dependency; deepagents
  remains below 0.8.

Verification recorded on Windows, Python 3.11.13, deepagents 0.7.10:

- Repository offline CI selection: 2,848 passed, 5 skipped, 11 subtests passed
  (`.tmp/factory-ci.xml`). This preceded the final startup, SDK retry/output-bound
  and documentation transport changes; a fresh focused run covers those changes.
- Provider SDK/catalog/transport selection: 292 passed, including actual SDK request
  payload inspection without provider network calls.
- Previous focused factory/compatibility run: 239 passed (`.tmp/factory-final.xml`).
- Final current-code selection: 522 passed, three upstream deprecation warnings,
  in 134.20 seconds (`.tmp/factory-final-current.xml`). Covers every factory test,
  provider SDK/catalog/transports, session/reset and Prime compatibility, handoff,
  sandbox, process/container workers, team approvals/recovery and deepagents compatibility.
- Factory mypy: 14 source modules passed with `--follow-imports=skip
  --disable-error-code=misc`. Factory source/tests and selected integration files
  pass Ruff; 21 factory source/test files pass formatting.
- Final diff whitespace check passed. Existing user edits, attachments and the
  separate coordinator-recovery prerequisite were preserved.
- Four independent review rounds found execution-environment version mismatches
  and conversation-reset races. Fixes include private-environment metadata checks,
  immutable originating-thread fencing and startup-reset regression coverage.
  The fourth review preceded the final startup fix; no final reviewer approval is claimed.

Offline single-sample comparison (`.tmp/factory-benchmark.json`):

| Scripted workflow | Elapsed | Sampled peak process-tree RSS | Acceptance |
| --- | --- | --- | --- |
| Sequential reference | 1.0561 s | 118,689,792 bytes | 1/1, observed failure then success |
| Factory | 10.1795 s | 124,755,968 bytes | 1/1, observed failure then success |

The factory invoked nine scripted roles with peak concurrency two. This reference
is direct scripted work, not live legacy Prime. Provider tokens, real-model quality
and provider costs were not measured; no savings are claimed.

Remaining limitations and unavailable validation:

- Linux and the complete test directory have not completed validation. Broad runs
  encountered unrelated existing timeout failures; the identified decisions test
  passed independently with a longer timeout.
- Live provider/documentation/PR calls and a real Docker daemon were not exercised.
  Publication tests are offline; nothing was pushed or published.
- Active-time accounting checkpoints normal exit and excludes all-human waits.
  Abrupt host death can lose the interval since the last durable clock checkpoint;
  reconstructing that uncertain interval remains incomplete.
- Implementation requires a repository-root workspace and committed baseline.
  Integration conflicts preserve branches and pause for reconciliation. Windows
  worktree fallback uses the existing OS sandbox rather than full kernel containment.
- Explicit goals may contain issue details; a dedicated issue picker/fetcher is absent.

Existing user changes and attachments remain preserved. Validation performed no
release, push, merge, deployment or credential-dependent publication.
