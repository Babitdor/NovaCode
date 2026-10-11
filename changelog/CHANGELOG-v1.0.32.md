# NovaCode v1.0.32

## Added

- Cross-project communication between live tabs in one Nova window: use `@tab:<session-id> <message>` or `@<unique-title> <message>`, with autocomplete, queued delivery and linked replies. Agents can list sessions, send messages, read owned requests and explicitly reply through four session tools.
- Linked replies render as Markdown in initially collapsed panels. **Attach to prompt** adds the final answer to the owning tab's draft without sending it or interrupting a task; **Open tab** navigates explicitly.
- `/tabs resume` picks a saved session across projects; `/tabs resume <id>` accepts an exact ID or unique prefix. Sessions reopen in their saved project with their history, assistant, model continuation and checklist. Already-open sessions reuse their tab.
- Reproducible cross-session regression, coverage, baseline-probe and fault-injection checks, with separate implementation evidence reports.

## Fixed

- Linked replies retain the latest main-agent answer instead of concatenating intermediate and subagent output into a duplicate raw transcript block.
- Child sessions save after each turn, including interruption and error paths. Cancelling subprocess startup removes the incomplete tab.
- Child shutdown closes stdin before waiting for process exit, releasing outstanding pipe reads.
- Telegram topics use `NovaCode · <session name>`. Existing topics update on reconnect while preserving their history and routing; unchanged titles avoid repeated edits and failed edits can retry without creating duplicate topics.
- Telegram and peer turns use correlated prompt IDs so queued replies and attachments remain associated with the correct request. Unicode tab addresses are supported and ambiguous names fail clearly.

## Limits and validation

- Cross-session communication covers live tabs in a single Nova window. It shares only explicit message content; each recipient retains its own workspace, history, model and tool approval settings. Message bookkeeping is bounded and held in memory.
- Resuming requires an existing, Nova-approved project folder. Historical tools are displayed without executing them. Model availability and legacy-session fallbacks retain the existing continuation behavior.
- Targeted Windows session, tab, Telegram, transcript and model-restoration regressions pass using simulated model events, mocked Telegram APIs and real child-process transport rehearsals. New communication and resume modules have 100% line coverage.
- No live-provider or live-Telegram validation was performed. Existing full-suite timeouts and repository-wide lint limitations are documented in `CROSS_SESSION_EVIDENCE.md` and `TAB_RESUME_EVIDENCE.md`.
