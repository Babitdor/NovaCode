# Coding reliability experiment evidence

## Contract and source state

Contract: [coding-reliability-spec-2026-10-14.md](coding-reliability-spec-2026-10-14.md).
Spec approval: not obtained (autonomous run after the user's "Proceed, with
recommendations"). No new dependency, release, or push.

The worktree contained extensive unrelated uncommitted changes before this
experiment. This report concerns the local changes to `task_discipline.py`,
the `core_agent.py` opt-in wiring, their tests, the evaluation analyzer and
comparison command with their tests, plus the evaluation README.
The base commit is `f865a2a47a5162722552a78bd762eb8eb0b76768`; the final
working-tree state is uncommitted. SHA-256 of the changed production files:
`task_discipline.py` `D764A3C4A0B95B7B44C4AEA67F33AFC37BE30544393214DB629BA5E55498A52F`,
`core_agent.py` `75BB2113984710D03DE4D73A29C1A3D6020DFA4DC5F261C6E27ECF3D75971467`,
`analyze.py` `8D382AC7B88A7C53FB3E517C3665B0E4A39D7F59C31ABC6EB21E690F22191678`,
and `compare_jobs.py` `2CD3B539011205A7B2E4474664A8995F4BF2CA90B7A526F31D3353A67955A921`.

## Baseline observations

- Terminal Bench 2 historical run `jobs/tb2/2026-10-13__21-51-25`: 49/89
  passed; 27 verifier task failures. The new offline detector found 20 of those
  27 had an observed file-edit tool result without a later recognized check
  command. **Thirty-three of 49 passing trials had the same signal.** It does
  not predict failure or establish why any task failed. Many tasks validate
  through custom shell scripts the narrow detector cannot recognize.
- SWE-bench Verified pilot: 5/5 passed. One of those five also lacked a later
  recognized check command, showing that the signal is not a failure label.

## Verification

- RED: new task-discipline test failed because an edit followed by completion
  returned no reminder; new analyzer test failed because `Trial` lacked the
  post-edit field. A malformed-observation negative control also failed before
  the parser was hardened.
- Focused tests after implementation: `46 passed` across
  `tests/test_deepagents_compat.py` and `tests/test_task_discipline.py`,
  including a scripted actual agent graph; `15 passed` across the evaluation
  analyzer and paired comparison tests.
- Scoped lint: Ruff passed on `task_discipline.py` and the changed evaluation
  source/tests with `INP001` ignored because `novacode_cli/agents` is an
  existing namespace package. The touched `core_agent.py` has many pre-existing
  Ruff findings from other worktree changes; its edited line passed a scoped
  Ruff format check. The other changed Python files passed format check.
- Targeted mypy: one new error was found and fixed. Five errors on unchanged
  pre-existing lines remain in `task_discipline.py`; the command is not green.
- Two preliminary live replays used the wrapper's default reasoning setting,
  while the historical Terminal Bench run used `reasoning_effort=off`. They are
  **not matched comparisons**. `sparql-university` scored 1 versus historical
  reward 0. `distribution-search` scored 0 versus historical reward 1, but its
  replay made zero tool calls and emitted four empty output-limit responses
  (65,536 output tokens); the edit reminder could not have fired. These runs
  revealed the configuration mismatch and an independent truncation-recovery
  weakness.
- Matched replay `jobs/tb2/2026-10-14__11-28-45` used the same model, dataset
  hash `sha256:c6fc2e2382c1dbae99b2d5ecd2f4f4a60c3c01e0d84642d69b4afd92e99d078b`,
  Modal environment, task revisions, and `reasoning_effort=off` as the baseline.
  It scored 2/2 with the verification reminder enabled at the time:
  `sparql-university` changed from 0 to 1 and
  `distribution-search` remained 1. The sample is too small and model paths
  differ, so this is an exploratory result rather than causal proof.
- Both matched traces ran custom Python validation after their final file
  edits. The narrow detector missed it and prompted an extra check. The
  reminder was revised after these runs to allow the agent to cite a prior
  check and its result. The revised wording has only offline test coverage;
  no live result is attributed to it.
- Final rollout: the reminder is opt-in through `NOVA_VERIFY_AFTER_EDIT=1`.
  The small matched panel did not justify the extra model turn in the default
  agent. The analyzer and profile-checking comparison command remain usable
  without that option.
- The new `compare_jobs.py` command rejected the unmatched run with an explicit
  `agent_kwargs` mismatch and reported the matched pair as one gain, zero
  regressions. It is offline and has no model calls.
- Full main suite: 4,006 passed, 13 skipped, 1 xpassed, two failed in 20m12s.
  Failures were a Hypothesis generation-speed health check in
  `test_context_learning_properties.py` and a Textual viewport `OutOfBounds`
  in `test_provider_signin_ui.py`. Both passed when rerun alone (2/2); these
  modules were not changed for this experiment. The full run overlapped the
  final prompt-wording and evaluation-script edits, so it is not a final-state
  all-suite pass. Focused tests were rerun after those edits.

## Limits

The gate and analyzer recognize common test/lint/build commands following
`write_file` or `edit_file`. They cannot prove command success, catch shell
edits, or understand arbitrary custom validation scripts. A reminder fires
once per user turn and lets the agent report an inability to verify.

Known limits: the live panel is two tasks, and no paired repeated-run estimate
exists. The gate can send an unnecessary extra model turn when a custom check
is already sufficient. The analyzer records attempted commands, not their
success or relevance. A larger randomized A/B run is needed before claiming
measurable reliability improvement.

No dependency set changed, so supply-chain audit was not applicable. Changed-line
coverage, mutation testing, and randomized suite order were not run. The RED
tests and the real mismatched-job negative control prove only their specific
failure paths. The full suite's load-sensitive failures limit confidence in a
single all-suite run.
