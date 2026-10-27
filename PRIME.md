# Nova Prime · Deep Work

Prime is an opt-in workflow for a Nova conversation. It makes completion criteria,
investigation, verification and a counterexample review visible as checkpoints.

```text
/prime on       Enable Deep Work for the next request
/prime off      Return subsequent requests to the normal workflow
/prime status   Inspect the mode and the last request's progress
/prime report   Read the last delivery record
/prime          Toggle the mode
```

For a new TUI or headless process:

```text
nova --prime
nova --prime -p "Fix the expired login cookie and add a regression test"
nova --no-prime --continue <session-id>
```

The explicit CLI flag overrides the resumed session's preference. Without a
flag, resuming preserves its saved mode. New conversations default to off.
Changing the mode starts no work and applies to the next foreground request.
The **Prime: off/on** button beside the composer toggles the same session setting
with a click or keyboard activation, preserving the current draft. While enabled,
the composer has an accented frame labeled **Nova Prime · Deep Work**, a highlighted
toggle and a phase badge. Colors follow the selected theme; Plan, Bash and
auto-approval indicators keep their established colors. Composer controls use two
rows on narrow terminals.

## The workflow

1. **Define:** derive concrete completion criteria from the request.
2. **Investigate:** inspect relevant source, conventions and existing tests;
   reproduce a bug or identify its precise failure before editing.
3. **Implement:** make a focused change within the authorized scope.
4. **Verify:** run suitable checks, diagnose failures and fix them within the
   existing limits. State what could not be checked.
5. **Challenge:** look for an edge case, counterexample or regression that could
   defeat the proposed solution; exercise it when practical.
6. **Deliver:** summarize the change, checks, limitations and next steps.

Prime uses the existing agent/tool loop and selected model. The model records
each phase with `prime_checkpoint`; the prompt badge and transcript show progress.
Criteria become fixed after Define. New scope still needs an owner decision.
For explanations or planning, irrelevant implementation/checks are explicitly
marked as not applicable or deferred. Planning mode remains read-only.

The checkpoint tool accepts only the current phase and evidence references to
actual tool-call IDs observed in the owning foreground request. The model sees
recent observed IDs so it can cite them accurately. Another conversation or
nested subagent cannot advance the parent's checklist. Investigation and test
quality still depend on the agent's work; recording a milestone is not a proof
that every preceding action followed the guidance.

## Delivery records and limits

The delivery record separates **agent assessments** from **observed tool results**.
It includes completion criteria, verification notes, counterexample review and
unverified work. A successful tool return alone does not certify a passing test
suite; Prime does not replace inspecting the underlying test output.

- **Delivered:** all six milestones were recorded with execution evidence.
- **Delivered with limits:** all milestones were recorded, with explicit
  verification limitations or checks that did not apply.
- **Incomplete:** the agent ended without all required milestones.
- **Paused/error:** execution was interrupted or failed; no automatic replay occurs.

Verify must reference an observed successful execution-tool result (such as
`shell`, `execute`, `execute_code`, `run_tests` or `run_pytest`), or explicitly
explain why execution evidence is unavailable. These are execution observations,
not an independent interpretation of arbitrary test logs. Challenge is the
agent's self-review; Prime does not silently launch an external reviewer.

Deep Work can use more model/tool steps. Existing recursion, goal, headless turn
and time limits still apply. The host-generated delivery record consumes no
additional model step. Prime does not switch providers, raise reasoning effort,
enable auto-approve, grant tools, authorize integration, or create background jobs.
Claude and Codex room conversations retain their own controls.

## Sessions

The enabled flag and last thread-bound checkpoint snapshot are saved with normal
session metadata. Old sessions default off. Resuming an interrupted record shows
it as paused; a new request starts a fresh checklist using the conversation's
existing context. No uncertain action is automatically retried.

`/clear` removes the old evidence while preserving the explicit Prime toggle,
like other persistent conversation controls. Late results from a cleared or
superseded request cannot repopulate its progress. A new session has its own
independent mode and report.

## Offline checks

```text
uv run pytest tests/test_prime.py tests/test_prime_ui.py tests/test_deepagents_compat.py tests/test_headless.py tests/test_autonomous_loop.py tests/test_session_state_fields.py -q
uv run ruff check novacode_cli/prime.py novacode_cli/agents/prime.py novacode_cli/commands/prime_handler.py tests/test_prime.py tests/test_prime_ui.py
uv run mypy novacode_cli/prime.py novacode_cli/agents/prime.py novacode_cli/commands/prime_handler.py --follow-imports=silent
```

Tests use fake models through a real LangGraph run, temporary session storage and
Textual pilots. No provider calls or new dependencies are required.
