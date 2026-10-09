# NovaCode v1.0.29

## Added

- Persistent public `--mode pipe` / `--headless` JSONL sessions for remote-app bridges, with readiness, request IDs, tool approvals, cancellation, queues, and clean shutdown. Includes process-local `--model`, startup/request/approval deadlines, a documented v1 protocol, and a Node adapter example.

- Telegram toggle in the `/trello` board header connects a dedicated Trello topic using the current remote bridge or saved protected credentials.
- Authorized users can send text tasks or `/add <task>` to create Loaded cards, and `/status` to see counts. Topic messages never become ordinary session prompts or steering.
- Topic links, asynchronous connection status, duplicate message protection, deleted-topic recovery, and paused intake when the toggle is off or the board stops.
- Headless deadlines (`--timeout`), optional partial text events, versioned JSON records with exit codes, bounded UTF-8 input, and structured startup errors.

## Fixed

- Internal compaction uses the threshold shown by `/context`; category estimates rebuild after compaction and resume. Internal SESSION INTENT drafts are hidden behind an animated compaction status.
- Startup restore seeds a fresh checkpoint instead of merging saved history with old messages and summarization cutoffs. Restore budgets count tool arguments and retained reasoning, and restored metrics include both the briefing and injected instructions/memory.

- Trello execution and logs stay with the board's owning TUI tab. Queued execution waits until that tab is selected and idle.
- Telegram task intake preserves the session's approval mode, existing topic subscriptions, and dedicated handlers across bridge restarts. Connection and acknowledgment operations have deadlines.
- Notification hook payloads identify TUI-managed delivery and provide the current installation's Nova.png path. The example Windows hook defers to the native TUI notifier to avoid a second toast with the old avatar.
- Pipe output survives partial writes and interrupted writes, routes ordinary/native/child diagnostic output to stderr, and reports cancellation, timeouts, closed pipes, and incomplete event streams consistently.
- Headless approval requests reject unresolved verdicts rather than silently granting permission. `--print` no longer enables auto-approve or persists recursive workspace trust. Use explicit `--auto-approve` and, when needed, `--trust-workspace` (folder-only).
- Subagent prose no longer pollutes the final headless answer or turn count; exceeding the turn budget cannot be hidden by an interrupt or completion event.

## Limits

- The board remains in memory; keep Nova and the board running. The dedicated topic accepts text tasks only.
- Telegram must already be configured through `/remote`; forum topics and bot Manage Topics permission are required.
- Existing user-installed PowerShell notification hooks need the updated `examples/hooks/windows-notify.ps1` to honor the new payload fields.
