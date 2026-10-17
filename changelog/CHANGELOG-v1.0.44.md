# NovaCode v1.0.44

## Added

- `/update` has an **Update now** button: it saves the session (stopping a running turn after asking), installs the update in the same terminal, restarts Nova and reopens the session, continuing an interrupted turn. A failed update restarts the current version.
- `/claude-review` and `/codex-review`: Claude Code or Codex reviews the changes headless and read-only; Nova verifies the findings and reports them. Both resume with `resume` and report progress to Telegram.
- `/claude-plan resume` and `/codex-plan resume` continue a planning stopped with Esc or `/stop`.
- Settings shows Claude Code and Codex usage (5-hour and weekly windows) as progress bars, with a **Check Claude now** refresh.
- Message queueing, interrupt-and-redirect (`/redirect`, also from Telegram), pulling a queued message back with Up, and Up/Down input history.

## Changed

- `nova update` shows animated stage progress and a finish screen without clearing the terminal. On Windows it runs in the foreground (the running launcher is moved aside), so the shell prompt returns only when it is done.
- Per-call tool schemas are 55% smaller (45 tools / 57k characters to 25 / 25k): most-used tools are ranked on the last 30 days, the async agent roster is one line per agent, rarely called tools load through `search_tools`, and long descriptions are shorter.
- The NOVA wordmark shows in windows narrower than the full banner and in windows from 17 rows tall, instead of disappearing.
- `wait_for_task` is a short look of at most 10 seconds, once per user message.

## Validation and limits

- Focused suites for updates, planners, reviews, banners, tool search, queueing and the TUI pass; the full suite was not run.
- Update now and the in-place Windows update were tested with stand-ins and a throwaway uv tool, not yet on a real Nova install.
- Only the main session is reopened after an update; extra session tabs are not.
