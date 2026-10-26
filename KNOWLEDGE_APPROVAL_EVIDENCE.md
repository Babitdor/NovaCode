# Knowledge cache and teammate Auto Approve verification

Task contract: `KNOWLEDGE_APPROVAL_SPEC.md` and the supplied implementation plan.
Spec approval: not obtained separately (autonomous implementation request).
This report describes focused executable evidence, not a claim of complete proof.

Final verification command:

```powershell
.venv/Scripts/python.exe -m scripts.verify_knowledge_approval --mutations
```

Result: exit 0. **360 passed, 1 skipped**, with three existing dependency
deprecation warnings. The skipped test creates real symlinks; this Windows account
does not have permission to create them. Sixteen selected source/test/script files
passed Ruff lint and format checks. The three new modules passed strict mypy with
`--follow-imports skip`. `git diff --check` passed. The runner prints all commands,
dependency versions and the hash of its listed task sources, tests and integrations.

Final source hash:
`7f351086824ad90ee4b147745f1576ddd1796326582cf54aea44727bdc50f007`

Repository baseline: `eaff7cc5f32df5a50cc64651c1c2beca3535b376`.
The task changes are uncommitted. Unrelated working-tree edits were preserved.
Recorded tools: pytest 9.1.0, Ruff 0.15.17, mypy 2.1.0,
langchain-core 1.6.6, deepagents 0.7.10. No dependencies were added or changed.

## Acceptance evidence

| Behavior | Executable evidence |
| --- | --- |
| Persistent exact reuse without embeddings; query-independent generation | `test_cold_warm_restart_and_exact_without_embeddings` |
| Semantic top-three retrieval and relevance threshold | `test_paraphrase_retrieval_and_backend_authority`, `test_semantic_top_three_and_threshold` |
| Workspace/assistant/model/prompt isolation and immediate scoped toggles | `test_model_prompt_and_assistant_isolation`, `test_workspace_isolation_and_scoped_switch`, `test_cache_command_uses_current_backend_workspace` |
| Dirty/untracked changes, unchanged timestamps, complete hashes and deletion | `test_hash_validation_ignores_timestamps_and_deletion`, `test_complete_hash_and_expiry` |
| Backend authority, traversal, private/credential exclusions | `test_paraphrase_retrieval_and_backend_authority`, `test_private_or_escaping_sources_rejected`; real symlink case skipped as above |
| Clear/off during generation; source changes before publication | `test_clear_and_off_fence_inflight_generation`, `test_source_changed_during_generation_never_published` |
| Producer concurrency, stale leases, cancellation | `test_concurrent_producers_share_one_generation`, five independent-store repetitions, `test_store_expiry_lru_and_stale_leases`, `test_cancellation_releases_producer` |
| Bounded source/summary payloads, expiry, quota/LRU eviction, corruption | `test_limits_and_corruption_fall_back`, `test_invalid_model_response_not_published`, `test_complete_hash_and_expiry`, `test_store_expiry_lru_and_stale_leases` |
| Deadline and provider text-block compatibility | `test_summary_deadline_is_recoverable`, `test_provider_text_blocks_exclude_reasoning` |
| One cold call/token attribution and zero warm calls | `test_cold_usage_attribution_and_zero_warm_calls` |
| Main-agent-only core registration and local-only embedding loading | `test_core_tool_stays_out_of_delegated_rosters`, `test_local_embedding_loader_never_falls_back_to_repo_id`; existing VM graph tests assert its exact four-tool roster |
| Auto Approve default-off, strict boolean, persistence, legacy recovery and isolation | New tests in `test_teammates.py` and `test_teammate_registry.py`, including legacy records without the field |
| Exact durable intent, decision source, single execution and uncertain recovery | `test_automatic_input_records_intent_source_and_single_result`, `test_auto_approve_uncertain_action_never_replayed` |
| Existing pending input unchanged; disable, pause, takeover, observation gates | `test_enable_does_not_approve_pending_and_disable_gates_next`, `test_auto_approve_control_change_during_frame_preparation` and existing computer/input regressions |
| Authenticated owner command and preview policy | Parameterized loopback authentication tests and multi-agent preview service test; preview transport rejects the setting command |
| Polling, failed-save rollback and pending-review display priority | `test_auto_approve_switch_polling_and_failed_save`, controller failed-save test, existing small-terminal UI regressions |
| Headless, usage, security, computer, code-mode and dependency compatibility | The runner's sixteen-file regression list |

Five explicit mutations were executed and killed by assertion failures: incomplete
source hashing, removed credential exclusions, accepted tool-calling summaries,
unconditional automatic approval, and missing observation invalidation. Every
mutated file was restored byte-for-byte in `finally`; no mutation remains.

The final synthetic offline replay recorded one cold summary call, zero warm
summary calls, 4,100 source-context characters versus 407 warm-context characters,
and misses for stale and unauthorized fixtures. These are synthetic fixture
measurements, not measured production savings or provider billing estimates.

The Work tab was also rendered and inspected at 100×40 and 60×30 cells. Pending
review precedes the setting, and approval controls remain visible. The standalone
preview check and UI tests passed together (12 tests). The optional design detector
reported only pre-existing preview styling and dynamically populated image tags;
those unrelated structures were preserved.

## Limits

No live-provider call, real VM action, actual Model2Vec paraphrase-quality benchmark,
Linux permission run, or full repository suite was performed. Model/VM boundaries
use offline fixtures; cosine thresholds and retrieval logic use controlled vectors.
The opt-in provider smoke procedure is documented in `KNOWLEDGE_CACHE.md`.

No changed-line coverage threshold, additional property-based campaign, or
independent-agent review was run. Validation was scoped to relevant regressions,
static checks, direct rendering, offline replay and five adversarial mutations.
This is a declared reduction from the old-coder skill's full gauntlet, following
the task's focused checks and the requirement to run the smallest sufficient proof.
