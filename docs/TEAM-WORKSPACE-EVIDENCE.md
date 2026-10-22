# Team workspace and packaged prompt checks

Spec approval: not obtained (autonomous run). This report records checks written
and run by the implementation author, not an independent security audit.

## Behavior and evidence

| Contract | Evidence |
| --- | --- |
| Project writes retain auto-approve | `test_in_project_write_stays_automatic` |
| External writes require an actual decision | `test_external_write_requires_real_approval_even_in_auto_mode`, `test_explicit_external_call_approval_is_preserved` |
| Explicit path grants survive; denies win | `test_explicit_path_allow_grants_external_destination` |
| Virtual, absolute and relative project paths resolve correctly | `test_virtual_relative_and_absolute_project_paths`, 20 generated path cases |
| Traversal retains the external destination | `test_traversal_keeps_its_external_destination` |
| Mounted global memory cannot silently receive files | `test_mounted_external_write_does_not_execute_without_approval` checks real file side effects, denial and approval |
| Failed or changed resolution prevents execution | `test_changed_destination_during_approval_never_executes`, both cases of `test_unresolvable_destination_never_executes` |
| The worker cwd controls worktree containment | `test_write_boundary_uses_worker_root_and_rejects_invalid_envelopes` |
| Read-only tools are unchanged | `test_reads_have_no_write_destinations` and existing team tests |
| Approval cannot be replayed or outlive policy changes | Existing team approval tests plus the external-call replay assertion |
| Main templates come from the package | `test_main_templates_ignore_global_overrides_and_activated_artifacts` |
| Other prompt customization remains available | `test_non_main_template_keeps_explicit_activation` and prompt-evolution tests |
| No shared cwd mutation or new dependency | Diff review; existing in-process/process tests; lock diff contains only the release version |

## Checks run

- RED: the first workspace test collection failed because the resolver did not
  exist. The packaged-prompt test then failed against activated replacement
  instructions before the renderer change.
- Expanded team, prompt-contract, lifecycle and prompt-evolution selection:
  **72 passed, 1 skipped**.
- CI offline selection plus lifecycle/prompt-evolution: **439 passed, 2 skipped,
  1 failed**. The only failure identified the still-old `_version.py` constant.
  Corrected it to match pyproject, lock and changelog.
- Final focused workspace/approval/prompt/version selection, including the new
  error-path tests: **27 passed, 1 skipped**.
- Coverage across the broad run and focused follow-up: workspace resolver
  **100% (48 statements)**; team middleware **97% (68 statements)**. The two
  uncovered middleware lines are existing peer-message acknowledgment paths.
- Three deliberate faults were killed by the tests: skip outside containment,
  ignore the canonical path deny, substitute a different destination. These
  were applied in disposable test-process modules; the working implementation
  was not mutated. This is a targeted manual mutation check, not exhaustive
  mutation coverage.
- Ruff passed for the new resolver, team middleware and changed/new tests.
  Focused F/I lint also passed for the coordinator. Mypy passed for the resolver
  with imported modules skipped. Compileall and git diff whitespace checks
  passed. Existing large modules were not reformatted.

## Limits and unverified behavior

The Windows host cannot create the real directory symlink used by one test;
that test skips here and is included in Linux CI. Mount routing, changed-target
and resolution-failure tests do execute locally. One isolated run timed out in
Python's Windows platform probing during dependency import; the focused rerun
with a 120-second test timeout passed. Filesystem property tests have no timing
deadline because Windows path resolution is I/O-dependent.

The complete repository suite was not run. The offline CI selection and focused
team suites were used; prior full-suite work established unrelated TUI banner
failures. This change is not an OS sandbox for shell, Python, plugins or MCP
tools. A filesystem race remains after the final path check. No live provider,
container deployment or user Research directory was used for these checks.

Prose selections from historical conversations are not parsed into blanket
folder grants. External file calls remain possible through explicit parent
approval or configured path allow rules. Global agent.md continues to supply
memory; it is not loaded as the base main-agent definition. The user's Codex.md
reference was read and preserved without adding it to this change.

## Follow-up: supplied standing-instructions reference

The user subsequently supplied `novacode_cli/prompts/agent.md` as the desired
default style. Its general standing rules were adapted into the packaged
`nova_standing_instructions.jinja`, shared by startup and continuation. Neither
reference file was modified or committed. The private/global memory surface
remains separate from these release-controlled templates.

- Main prompt, contract, version and related updater checks: **32 passed**.
- Entire updater workflow test selection after isolating automatic background
  requests: **54 passed**. An explicit mock guard asserts no real update JSON
  request occurs in update-restart tests.
- On the v1.0.80 push, GitHub regressions passed on Linux (**418 passed**) and
  Windows (**417 passed, 1 skipped**). The Windows install/update job exposed an
  existing unmocked automatic update request during TUI test teardown. It timed
  out in Windows certificate loading. The follow-up disables automatic update
  checks in those offline tests; manual checks remain explicitly mocked.
