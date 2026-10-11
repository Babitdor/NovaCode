# Cross-session communication: implementation and verification

Validation began against `25365f0` on 2026-10-11, before the v1.0.32 release commit. No paid model call or Telegram network call was made during verification.

The table below records the reply-panel follow-up. The subsequent saved-tab feature and expanded verifier results are documented separately in `TAB_RESUME_EVIDENCE.md`.

## Behavior

Use `@tab:<id> question` or `@<unique-title> question` in a Nova tab. Autocomplete includes explicit addresses. The sender keeps running and the selected tab stays selected. Busy recipients queue requests. Both transcripts show linked request status and replies in initially collapsed Markdown panels. Expanding a panel stays in the current tab; its explicit **Open tab** button navigates to the peer. **Attach to prompt** appends the final answer with message provenance to the current draft without sending it or interrupting a running turn. Hidden panels cannot change another tab's draft. Status updates preserve expansion state.

The destination also retains its normal agent transcript. Previously, the linked reply duplicated that output as a large raw cyan block, including concatenated intermediate messages. Linked panels now retain the latest main-agent answer and exclude subagent answers, while the destination's normal transcript remains intact.

The parent binds sender identity to the root turn or child pipe. Four tools list sessions, send messages, read owned requests/results, and reply to a running owned request. Recipients retain their own agent, project, history, model, plan mode and approval preference. Only explicit message content crosses projects. Remote and compiled subagent definitions retain their existing configuration.

Scope is live tabs in one window. Message bookkeeping is in memory and cleared on shutdown. Replies do not start another model turn automatically. Peer slash-command text is context; ordinary Telegram slash commands retain their command dispatch.

Limits: 32 pending requests per destination, 256 retained messages, 8 MiB retained UTF-8 payload, 16 KiB request, 64 KiB response, eight request-chain hops. Worker RPC has 16 outstanding requests and a ten-second timeout. Completed entries can be evicted before pending entries.

Review also found and fixed an actual child shutdown race: cancelling `to_thread(stdin.readline)` leaves the underlying read blocked. The supervisor now closes child stdin after its shutdown frame, before waiting for exit. A regression failed before this fix and the real transport rehearsal verifies normal zero-code exits afterward.

## Executed checks

| Check | Result |
| --- | --- |
| Focused feature and existing session/Telegram regression suites, including reply panels | **136 passed**, 82.71 seconds |
| New communication modules, line coverage | **100%**, 471 statements |
| Coverage with branch measurement | **99%** overall; broker 100%, gateway 97%, TUI delivery 98% |
| Coverage gate | 90% passes; deliberate 100% gate fails as expected |
| Five manual fault injections (initial feature verification) | **5/5 killed** by assertion/test failures; source restored after each |
| Strict configured mypy on the three new production modules | Pass with `--follow-imports=silent` |
| All configured Ruff rules on the three new production modules | Pass |
| Ruff fatal/syntax checks on affected production/test/verifier files | Pass |
| Formatter on initial 13 new Python files plus reply-panel tests; `git diff --check` | Pass |

Acceptance evidence includes duplicate names and stable IDs, file/subagent collision preservation, Unicode text and project paths, concurrent endpoint isolation, FIFO requests, immediate replies during pipe drain, foreign replies, wrong completion IDs, queued Telegram/peer separation, model error/cancellation correlation, hidden-main-tab and plan-mode state, shared turn locking, Ctrl+C stream cleanup, RPC cancellation/timeout/shutdown, queue/byte/chain limits, eviction and recipient exit.

Seven reply-panel regressions verify Markdown rendering, initial collapse and expansion preservation, explicit tab navigation, draft preservation and attachment deduplication, no automatic dispatch or running-turn cancellation, hidden-panel ownership, disabled attachment without a response, and final-answer selection excluding intermediate/subagent output. The combined run initially exposed a test click before layout settled; waiting for layout and asserting successful physical clicks resolved it. The final combined run passes.

The transport rehearsal launches two **real Python processes in separate Unicode project directories** through the production worker/supervisor JSONL transport. Only model event production is replaced. It checks sender/reply ownership, workspace paths, unchanged approvals, process exit codes and completed reader tasks. It uses no credentials or network calls.

Red evidence was observed for the initial broker and delivery specification (six and three failures respectively), the stdin-before-wait regression (one failure), Unicode explicit Telegram addressing (one failure), and failure-event correlation (two failures). Other tests were added after implementation; this is not a claim that every change followed a red-first sequence.

Mutants removed reply ownership, allowed queue overflow, silently accepted ambiguous titles, accepted the wrong prompt ID, and dropped the current endpoint. Each was rejected by its specific test. The verifier restores source bytes and timestamps in `finally` and reports mutation outcomes in JSON.

## Baseline limitations

The original environment's DeepAgents 0.6.7 cannot collect nine existing tests; Nova declares 0.7.x. Validation used an isolated `.tmp/cross-session-env` with DeepAgents 0.7.10 and compatible dependencies, leaving the global environment unchanged. Runtime: Windows, Python 3.13.5, Textual 8.2.7. No Python 3.11 runtime or live-provider validation was performed.

The full suite was attempted but aborted under its configured ten-second thread timeout. Both `test_the_fade_paints_the_buttons_own_fill_on_every_frame` and `test_tui_bg_agent_card_sizes_to_content` reproduce the same timeout against an extracted **committed baseline** and the candidate. Full-suite success is unverified. The baseline reproduction copies committed source; it does not replace the working tree.

The initial feature's repository-wide configured lint audit was not clean: baseline **11,814** diagnostics, candidate **11,994**, before the reply-panel follow-up. New test helpers contribute annotation/style diagnostics under the repository's strict all-rules configuration; these are not classified as pre-existing failures. The production-module and fatal/syntax checks above pass. No repository-wide strict-type-check success is claimed.

No latency, coding-quality, billing, or real-model behavior claims are made from these model-free tests.

## Reproduction

Use a Nova development environment satisfying `pyproject.toml`, with pytest, pytest-asyncio, pytest-timeout, coverage, Ruff and mypy installed. No new runtime dependency was added.

```powershell
python scripts/verify_cross_sessions.py
python scripts/verify_cross_sessions.py --mutations
python scripts/verify_cross_sessions.py --baseline
python -m mypy --follow-imports=silent novacode_cli/sessions/messaging.py novacode_cli/sessions/gateway.py novacode_cli/sessions/tui_messaging.py
ruff check novacode_cli/sessions/messaging.py novacode_cli/sessions/gateway.py novacode_cli/sessions/tui_messaging.py
git diff --check
```

Here, replace `python` with `.tmp/cross-session-env/Scripts/python.exe`. Outputs are in `.tmp/cross-session-verification`: `regressions.json`, `tests.xml`, `coverage.json`, `mutations.json`, `baseline.json`, per-check logs and both repository-wide lint JSON files. Baseline mode records expected failing probes and exits successfully as a diagnostic operation; it is not a pass/fail acceptance gate. Mutation mode temporarily changes source, so run it without concurrent tests or development processes reading those files.

The agreed acceptance cases are in `CROSS_SESSION_SPEC.md`. Remaining uncovered branches involve inactive/no-op cleanup paths; every new production statement is executed. No mutation-score claim beyond the five listed faults is made.
