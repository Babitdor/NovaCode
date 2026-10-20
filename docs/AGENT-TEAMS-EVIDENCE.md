# Agent teams: implementation evidence

Implementation date: 2026-10-20. Baseline revision:
`36e5c8369c4a3c58dadf48139042f7e4cd718218`.
The work remains local and feature-gated off. No dependencies were installed,
paid model calls made, container images provisioned or changes pushed.
Concurrent unrelated workspace changes were preserved.

## Delivered contracts

| Contract | Executable evidence in `tests/` |
| --- | --- |
| Root isolation, durable membership, atomic claims, lease expiry, inbox ownership | `test_team_store.py`, `test_team_properties.py`, `test_team_runtime.py` |
| Completed context, corrections, tool pairs, private snapshots and overflow | `test_team_runtime.py`, `test_team_inprocess.py` |
| Actual in-process graph through the shared agent loop | `test_team_inprocess.py` |
| Real subprocess launch and actual worker entrypoint with scripted graph/model | `test_team_process.py` |
| Exact model/capability/workspace/protocol handshake and bounded control frames | `test_team_process.py`, `test_team_container.py` |
| Real Git worktrees; preserve dirty and ahead work; independent container Git objects | `test_team_worktree.py`, `test_team_container.py` |
| Peer message during long tool, single execution and safe next-model delivery | `test_team_inprocess.py` |
| Current policy, explicit asks/denies, capability narrowing, cancellation and approval reuse | `test_team_approvals.py` |
| Selected cancellation, uncertain cleanup, owner fencing, `/clear` and multiple roots | `test_team_runtime.py`, `test_team_lifecycle.py` |
| Real process recovery: reused birth time or foreign nonce cannot stop another process | `test_team_recovery.py` |
| Legacy one-shot identity and restoration on success/exception | `test_team_legacy.py`, `test_team_lifecycle.py` |
| Manual-team default, opt-in DAG scheduler, no empty startup/polling model calls | `test_team_swarm.py` |
| Existing usage accounting, exact-once turns and unknown pricing | `test_team_accounting.py`, `test_team_inprocess.py` |
| Exact large Unicode final output, immutable artifacts, quota and tamper refusal | `test_team_output.py`, `test_team_process.py` |

This is executable contract evidence, not proof of real-model reasoning quality.
The process entrypoint tests replace only the model/graph with scripted fixtures;
they still exercise worker startup, transport, command handling and shutdown.
The recovery test launches and stops only its own harmless sleeping process.

## Verification results

| Check | Result |
| --- | --- |
| Final integrated regression, 28 files | **273 passed**, 109.63 seconds |
| Subsequent complete team regression, including recovery and legacy middleware | **52 passed**, 23.59 seconds |
| Targeted behavioural mutation probes | **6/6 caught** |
| Hypothesis generated state histories | 30 examples, passing |
| Ruff check and format, new team package and team tests | Passing |
| mypy, team package with `--follow-imports=silent` | Passing, 20 source files |
| Diff whitespace review | Passing |

The 52 team cases overlap the integrated regression; these counts must not be
added as independent coverage. Ruff/type results apply to the stated scope,
not the entire repository. No tests were weakened to obtain these results.
The final integration edits after that regression were formatting only.

Branch-aware parent-process coverage from the integrated run was **68%** for
the team package. Worker subprocess execution was not instrumented and appears
as zero in that report despite passing entrypoint tests. Container control,
crash recovery and legacy paths also have incomplete branch coverage. The later
recovery and legacy cases were not included in that coverage percentage.
No exhaustive mutation or full high-assurance coverage gate is claimed.

Six isolated, in-memory mutations removed root scoping, atomic claim protection,
lease expiry, inbox ownership, explicit deny or snapshot copying. Each caused
the intended behavioural test to fail. Production files were not mutated.

## Broad suite and baseline comparison

An offline sweep froze a manifest of 408 test files at launch and ran bounded
batches: **4,594 passing call outcomes, 34 failed/timeout call outcomes,
17 skipped/xfail outcomes and one teardown error**. This is an attempted broad
suite, not a green full-suite result. Four interrupted cases were recorded as
timeouts. Three were batch resource cutoffs and do not establish a per-test hang;
the Ctrl+B case hit its test timeout. Concurrent unrelated changes landed while
the sweep ran, so it is not a frozen measurement of one final workspace revision.

An unchanged Git archive of the baseline with the same installed dependencies
reproduced failures in async-delegation prompt text, audio install hints, desktop
sleep prompt text, learning recall and seven NVIDIA client-construction cases
(network discovery was deliberately blocked). It also reproduced four skill
versioning failures, task-discipline text, three session-reply-panel cases,
text selection, crash autosave and three home-banner widths. The Ctrl+B timeout
also occurred on baseline. The model endpoint failures seen in a separate
157-case run reproduced on baseline in the same file order: a test-imported
`_StubNovaConfig` lacks `set_model_config` (155 passed, two failed).

Not established as baseline failures: three user-async-agent roster cases,
three TUI transcript/todo/responsive cases, three TUI batch-cutoff cases and the
remote-session teardown error. These remain unresolved; no claim is made that
they are unrelated regressions. The teardown error reported `animation_fps`
changes in a temporary verification profile, not the user's real profile.

## Raw local artifacts

The sanitized [verification manifest](agent-teams-verification.json) records
results, file hashes and local evidence paths. The raw logs remain local in:

- `%TEMP%/nova-teams-final-nh8laa9p`: integrated and final team logs, XML,
  test manifests and coverage data.
- `%TEMP%/nova-team-suite-0hwbm93m`: fixed broad manifest, per-case JSONL,
  batch logs and aggregate failure list.
- `%TEMP%/nova-teams-baseline-6ev_xc9t`: unchanged archive and paired failure logs.
- `%TEMP%/nova-team-mutations-o60zm5g5`: six mutation results and logs.

Tests used temporary user profiles, removed provider credential environment
variables and blocked external network connections while allowing local
subprocess/loopback transport. No personal histories are included in fixtures.
Artifacts in temporary directories are machine-local and may later be removed
by the operating system; the sanitized manifest is the retained record.

## Remaining rollout gates

1. Shared immutable file-read caching is deferred. There is no validated cache
   benchmark or token-saving claim; normal fresh reads remain in use.
2. Restart is cleanup-only reconciliation. Resumable model-stream reconnect,
   adoption of live execution and restoration of pending approvals are not
   implemented. Uncertain tasks are blocked, never automatically replayed.
3. Container startup, provider access and cancellation against a real Docker
   daemon/image remain unverified. Offline tests cover argv, ownership/control,
   handshakes and independent Git metadata. Temporary container tool artifacts
   are not fully exported; mounted-worktree deliverables survive.
4. Swarm evidence comes from scripted tasks. Real-model task quality, conflict
   integration and mixed-transport failure scenarios need separate evaluation.
5. Whole-repository failures/timeouts and coverage gaps prevent an unconditional
   production-readiness claim. Keep the feature opt-in while these gates remain.
