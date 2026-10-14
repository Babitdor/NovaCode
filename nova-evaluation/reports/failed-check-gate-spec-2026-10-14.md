# Failed-check recovery: executable specification

Scope: the experimental `NOVA_VERIFY_AFTER_EDIT=1` gate. The benchmark trace
audit found that an attempted check can be mistaken for successful verification;
this change records the command outcome when it is available. It does not claim
to determine whether the check covers every requirement.

## Acceptance scenarios

1. Given an observed successful edit followed by a recognized check whose tool
   result reports a nonzero exit code, when the agent ends, send one internal
   reminder to inspect and address that failed check.
2. Given a failed check followed by a successful recognized check after the
   same edit, let the agent finish without a reminder.
3. Given a recognized check with no paired tool result, do not treat it as
   verification.
4. Given an observed successful edit and a paired successful check, preserve
   the existing no-reminder behavior.
5. Keep the reminder bounded to one per user turn, hidden from transcript
   replay, and disabled by default. Do not block legitimate failure reports.

## Setup and verification

No dependency or environment changes. Add focused unit and scripted graph
tests in `tests/test_task_discipline.py`; edit only the existing middleware and
its tests. Run RED, focused GREEN, scoped lint/format/type checks, and the
full suite if feasible. Review the diff. Match live benchmark replays by task,
model, reasoning setting, and environment before attributing any score change.

Spec approval: not obtained (autonomous run after the user's `Proceed`).

## Addendum: preservation invariant, identified during trace review

The historical `filter-js-from-html` verifier found five changed clean HTML
samples out of twelve, although the agent checked one clean sample and declared
formatting preserved. The live replay was launched before this addendum.

6. For a transformation that promises to leave unaffected inputs unchanged,
   NovaCode's general workflow must explicitly call for testing more than one
   structurally different unaffected input, with an exact comparison where the
   request demands exact preservation. Recheck after the final edit.
7. A scripted graph test must show the prompt reaches a model call. A matched
   live replay should test whether this guidance changes behavior; a single
   task result is exploratory and must not be presented as proof of a general
   score gain.

## Correction after inspecting the verifier

The addendum above is superseded. The task's clean-input verifier normalizes
the original with BeautifulSoup but compares it to the raw filtered output.
A byte-preserving filter can therefore fail. The current replay reported 31
byte-identical clean cases and still failed the same verifier comparison. The
preservation prompt addition and its test were removed. The partially launched
candidate replay was cancelled. No score gain is claimed from this task.
