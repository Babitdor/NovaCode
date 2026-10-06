# UI harness and session recovery validation

The implementation follows session-recovery-spec.md. Work proceeded under the
user's instruction to implement it; no separate specification approval was requested.

The focused Textual/session suite passed 119 tests. It covers prompt persistence
before streaming, final saves after failure, owner capture across tab switches,
unavailable checkpoints, abrupt subprocess exit, concurrent saves, failed atomic
replacement, torn compatibility files, metadata lag, legacy migration, and
Unicode round trips at six history sizes. Textual pilots exercise layout preview,
commit, rollback, reset, saved profiles, activity updates, and protected widgets.

The coverage gate passed 46/46 executable statements in atomic_write,
serialized_save, _save_snapshot, _load_snapshot_data, and _read_snapshot. Its
negative control returns failure. This is a scoped statement gate, not a claim
of complete branch coverage for the persistence module or the entire repository.

Three deliberate regressions were detected: non-atomic replacement, ignoring
the snapshot, and overwriting a conversation with an empty crash save. The
runner verifies a single behavioral failure using pytest's report; setup errors
and timeouts are rejected rather than counted as killed mutations.

Limited mypy checking passed for the persistence module, UI harness and UI tools
with follow-imports=skip, allow-subclassing-any, allow-untyped-decorators and
ignore-missing-imports. New modules/tests pass Ruff; existing touched modules
were compared with baseline diagnostics. CLI help exposes --safe-ui.

Reproduce with python scripts/verify_ui_recovery.py; use --mutations for the
mutation controls, or coverage run --branch followed by --coverage-gate.
Testing used the project's locked deepagents 0.7.10, langchain 1.3.18,
langchain-core 1.6.1 and langgraph 1.2.11 via a temporary dependency overlay,
because this machine's global deepagents differs and its project Python is
unavailable. Native audio warmup and online update checks are isolated.

The full repository suite and full dependency type checking were not run.
No runtime dependencies were added. Recovery restores the last completed disk
snapshot; disk failure, power loss, and uncheckpointed streaming tokens cannot
be guaranteed. Executable Python UI extensions remain outside this declarative
MVP. Concurrent unrelated workspace edits are excluded from the commit.
