# Memory wiki verification — 2026-10-19

At this verification snapshot, implementation was local and unpublished. No dependency was added and no live
model evaluation was performed. The specification was written before implementation;
separate spec approval was not obtained (autonomous run). These checks establish
the tested persistence/navigation contracts, not complete semantic correctness or
improved model reasoning.

## Retained behavior

| Contract | Executable evidence |
| --- | --- |
| Compact home, maps, related links, preserved source and preferences | `test_maintenance_indexes_links_and_preserves_sources` |
| Identical repeat performs no writes or new backup | `test_second_pass_is_idempotent` |
| Human-authored map is never overwritten | `test_existing_managed_name_is_not_overwritten` |
| Broken links reported; scoped pages reachable | `test_broken_links_reported_and_scoped_project_pages_reachable` |
| Habits-only/project-only stores recognized; canonical index used | `test_dream_handles_habits_and_uses_wiki_index`, `test_dream_finds_project_only_memory_and_preserves_scope` |
| Source snapshot exists before an already-organized dream | `test_dream_snapshots_an_already_organized_wiki` |
| New lessons survive navigation refresh; checklist remains stable | `test_new_lessons_survive_navigation_refresh`, `test_habit_reviews_keep_the_stable_checklist` |
| Overflow retains a full recoverable original | `test_compaction_keeps_an_exact_recoverable_original` |
| Stale/concurrent source is not overwritten | `test_stale_read_set_stops_before_writing`, `test_concurrent_write_is_not_overwritten` |
| Atomic failure, partial pass, resume and exact-byte restore | `test_atomic_failure_retains_source_and_backup`, `test_interrupted_pass_can_resume_and_originals_can_be_restored` |
| Path escape, malformed navigation and misleading completion rejected | `test_commit_rejects_outside_destination`, `test_malformed_or_contaminated_navigation_is_preserved`, `test_tool_reports_failed_maintenance_without_claiming_completion` |
| Agent binding and planning guard | `test_wiki_tool_is_bound_to_its_agent`, `test_dream_does_not_write_while_planning` |
| Research/project maps, encoded paths and example links | `test_maps_cover_research_other_projects_and_encoded_paths`, `test_check_ignores_example_links_in_code_and_remote_urls` |
| Generated maps do not displace factual recall | `test_generated_maps_do_not_compete_with_factual_memories` |
| Arbitrary Unicode bodies survive repeated maintenance | Hypothesis: `test_arbitrary_topic_text_survives_and_repeated_pass_is_stable`, 20 examples |

Additional focused checks cover existing memory tools, retrieval, search, prompt
ordering, approval persistence, Cowork enforcement and native TUI `/dream` routing.
New failure cases were reproduced before their fixes, including habits-only
discovery, checklist displacement, discarded overflow, newly recorded lessons
inside replaceable navigation, project-only discovery and generated-map recall.

## Results

Environment: Python 3.11.13, Ruff 0.15.17, mypy 2.1.0, coverage.py 7.16.2.
Base revision: `c68c58103379c617dff5cc2a7637771837dfaad9` (v1.0.46), with existing
uncommitted plan-storage work preserved. [Source hashes](source-hashes.json)
identify the working-tree files at that verification snapshot. The subsequent
prompt audit changes `longterm_memory.jinja`; see `../PROMPT_AUDIT.md` for its
separate regression evidence. These hashes are historical, not a release manifest.

```powershell
.venv/Scripts/python.exe scripts/verify_memory_wiki.py --coverage
```

Final result: **135 passed in 25.89 seconds**. The two new runtime modules have
181 executed statements, zero missing statements, 62 branch opportunities and
one partial branch: **99% combined coverage**, above the enforced 85% threshold.
This is coverage of the new engine/tool, not every changed line of the application.

```powershell
.venv/Scripts/python.exe scripts/verify_memory_wiki.py --mutations
```

Three isolated manual mutants were rejected by the intended assertions: removing
source preservation, removing the per-file concurrent-change guard, and returning
an empty broken-link report. The shared source was never mutated. This measures
those three defenses; it is not exhaustive mutation coverage or a proof of the
property suite in isolation.

Ruff passes for the new modules, handler, tests and verification scripts; formatting
checks pass for those files. Changed-line Ruff checks on the older integration
files report no introduced diagnostics. `git diff --check` passes.

```powershell
.venv/Scripts/mypy.exe novacode_cli/memory/wiki.py novacode_cli/tools/memory_wiki_tools.py novacode_cli/commands/dream_handler.py novacode_cli/hermes/memory_tiers.py --follow-imports=silent
```

Result: no issues in four files. The older `memory/agent_memory.py` retains five
type errors; the same five were reproduced from its untouched HEAD source
(lines 347, 351, 550, 665, 945; the final one shifts to 952 with this change).
They were not repaired as unrelated cleanup.

## Full-suite investigation and limits

The full suite was attempted with a temporary user profile and `--timeout=300`.
It encountered failures and then terminated at the timeout during the broader
background/TUI tests. **There is no full-suite pass claim.** The first attempt
also triggered an existing optional Hugging Face model fetch; the retained runner
sets Hugging Face/Transformers offline flags for subsequent runs.

An untouched detached checkout at the base revision and the current tree both
produce exactly two failures and 28 passes when running these files together:

```text
tests/test_async_general_purpose.py
tests/test_audio/test_speakable.py
tests/test_core_prompt_subagents.py
tests/test_context_learning_audit.py
```

The shared failures are:

- `test_prompt_prefers_async_general_work_and_yields_while_pending`: missing
  expected prompt phrase `return control to the user`.
- `TestCapability.test_install_hint_mentions_extra`: expected lowercase `voice`
  is absent from the current optional-dependency install hint.

Running `tests/test_ctrl_b_background.py -vv --timeout=45` separately on both
trees passes the first test and times out on
`test_bang_command_background_handoff_keeps_process_and_finishes`. Other failures
observed before the full-run abort were not individually classified; its aggregate
regression status remains unverified. The temporary baseline checkout was removed.

No randomized-suite plugin or automatic mutation tool was installed; these layers
were not run. Existing Hypothesis and coverage were used without adding packages.
No dependency/supply-chain change was made. Review of the diff found no new
credentials, publishing, provider calls or model changes. The new capability is
filesystem maintenance within an application-bound agent directory.

## Personal corpus repair

The authorized real-profile smoke run retained **94 topic pages**, five generated
maps, and an **894-character home index**. Link validation reports no unresolved
local links; an identical repeat reports zero changed files and no new backup.
Root instructions are 4,665 characters and the stable checklist is 1,804.

One-time repairs removed conflicting sleep windows, dangling checklist references,
stale tool/budget claims and project-specific assumptions from global standing
instructions. Exact original files and incident detail remain in timestamped
`history/wiki-backups/` snapshots, linked from the operating-history page.
Personal corpus files and backups remain under the user's `.nova` directory; private corpus text is excluded from the publishable diff.

Structural writes are individually atomic and check captured sources, but are not
a multi-file transaction. A non-cooperating writer can still race in the final
check/replace interval. Backups have no automatic retention/deletion policy.
Existing learning-ledger records and activated artifacts were not rewritten.
Real model adherence, recall quality and semantic-consolidation quality require
a separate live evaluation; the tests and smoke run establish structural behavior.
