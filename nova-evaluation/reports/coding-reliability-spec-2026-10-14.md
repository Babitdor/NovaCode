# Coding reliability experiment: executable contract

Status: autonomous implementation; spec approval was not obtained. The user's
"Proceed, with recommendations" authorizes this local experiment.

## Behavior

1. When a turn contains a successful `write_file` or `edit_file` call and the
   agent tries to finish without a subsequent check command, return it to the
   model once with an internal reminder to run a relevant check or explain why
   verification is unavailable.
2. A check before the last edit does not satisfy the reminder. A check after
   the edit does. A failed edit does not trigger it. A noncoding task does not
   trigger it. A new user turn resets the one-reminder limit.
3. The reminder must not assert that a check passed. It must permit blocked or
   unverifiable work to finish after one reminder. Existing todo and truncation
   gates must continue to work.
4. Offline trial analysis reports an observable "edited without later check"
   signal for grader failures, without presenting it as the failure's cause.
   Missing or malformed trajectories remain unknown rather than "not checked".
5. A scripted agent graph must show an edit, attempted completion, a reminder,
   a check command, then completion. Focused tests and lint must pass.

## Setup and limits

Use current Python dependencies and tests. Add tests in the nearby files and
this spec/evidence under `nova-evaluation/reports/`. No package installation,
release, push, or rewrite of existing worktree changes. The first experiment
counts observable checks, not their semantic relevance or success; bench
replays are needed before calling this a reliability improvement.

## Amendment after implementation review

Scenario 2's "noncoding task" phrase was too broad: the middleware cannot
reliably infer task type from a file-edit tool call. The executable boundary is
"no observed file-edit tool result". File edits to documentation or data can
therefore receive the same once-per-turn reminder. This is a known scope of the
experiment and should be evaluated for unwanted extra steps.

## Amendment from benchmark replay

Two exploratory replays accidentally omitted the historical
`reasoning_effort=off` setting. Add an offline paired comparison command that
rejects pairs with different model, agent kwargs, environment, task source, or
task revision. It may compare a subset of a larger baseline but must reject
duplicate task IDs and report gains and regressions per task. This prevents an
unmatched run from silently being presented as a harness improvement.

## Amendment from matched trace review

Both matched task traces contained custom Python checks after their final file
edits. The narrow command detector did not recognize those scripts, so the
initial reminder induced another check. Keep the conservative detector, but
allow the agent to cite the exact prior check and result instead of requiring
a redundant rerun. The reminder still fires only once.

## Final rollout decision

The historical detector signal also appeared in 33 of 49 passing trials.
Matched replay traces showed redundant checking, and a two-task panel is not
enough to prove a success-rate gain. Keep the reminder opt-in under
`NOVA_VERIFY_AFTER_EDIT=1`, with the normal NovaCode path unchanged. The
offline comparator and trace signal remain enabled by default.
