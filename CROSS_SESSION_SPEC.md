# Cross-session communication acceptance specification

Approved scope: the user approved the preceding five-step plan with “Proceed, but test properly. This is important!” This file concretizes that plan; no separate approval of this executable specification was obtained (autonomous implementation).

Scope: user addressing and agent communication among live tabs in one Nova window, including different project workspaces. No shared filesystem access, model-response caching, external messaging, new dependencies, automatic cross-window discovery, commits, or pushes.

## Acceptance cases

1. `@tab:backend question` resolves an exact live session ID or unique case-insensitive title. `@backend question` routes only when it identifies a tab without conflicting with a file/subagent. Unknown explicit addresses and duplicate titles report errors; file and subagent mentions retain their existing behavior.
2. Addressed input is intercepted before local busy-turn steering and before the exclusive local turn worker. The origin turn keeps running and the active tab does not switch.
3. Autocomplete includes explicit tab addresses alongside existing file and subagent completions. Stable IDs resolve duplicate titles.
4. A parent-owned broker binds sender identity to the connected worker/root context, validates recipient and reply ownership, and keeps independent per-recipient queues. It never accepts a caller-supplied sender or approval override.
5. Requests sent to idle recipients start a turn; requests sent to busy recipients wait in order. Only one cross-session turn per recipient executes at once. Root and child recipients use their own agent, workspace, history, model and approval setting.
6. Delivery, queued/running/completed/failed status, and linked replies are shown in the origin and destination transcripts in initially collapsed Markdown panels. Expanding a panel preserves the selected tab; an explicit Open tab button navigates to the peer. Attach to prompt appends the final answer with provenance to the owning tab's draft without submitting it or interrupting a turn. Hidden tabs are updated without rendering into or changing another tab's draft. Only explicit message text crosses the boundary; no implicit history/file/credential transfer.
7. Agent tools list sessions, send requests, read their own messages/results, and explicitly reply to requests addressed to their own session. Tools work over child-parent JSONL RPC and fail clearly outside a connected TUI session.
8. Completion/cancellation/crash is correlated to the exact delivered prompt/request ID. Replies are retained for the requester to read but do not automatically start another model turn. Empty, oversized, self-addressed, stale, forged, and unsupported requests fail clearly.
9. Pending messages are limited to 32 per recipient; total retained messages to 256 and payload bytes to 8 MiB. Requests allow 16 KiB and replies 64 KiB of UTF-8 text, with an eight-hop request-chain limit. Oversized inputs fail, completed entries may be evicted before pending ones, and shutdown/closed sessions release queues and RPC waiters.
10. Telegram addresses use the shared tab resolver; topic/reply/default routing continues for unaddressed messages. Session communication never changes existing tool approvals.

## Failure model and verification

- Misrouting/ambiguous names/file collisions: resolver cases and generated Unicode/name inputs.
- Races/duplicate delivery/deadlocks: concurrent sends, busy recipients, linked replies, and timeout/cancellation tests; RPC transport rehearsal with separate Python processes and project directories.
- Spoofing/privilege escalation: sender binding, foreign replies, unknown sessions, and unchanged approval flags.
- Lost state or terminal contamination: main/child and visible/hidden TUI integration tests; no implicit tab switch.
- Unbounded retention: queue/capacity/oversize/eviction/close tests.
- False confidence: red runs before implementation, targeted branch coverage with a nonzero threshold gate, plausible manual mutants, types/lint, existing regressions, and full-suite baseline comparison.

Setup: standard library and existing pytest/coverage/ruff/mypy tools only. Add broker, gateway, protocol integration, focused tests and a reproducible verifier/report. Read and edit locally; no git commit or push is authorized by this task. Real LLM/Telegram network calls are not necessary to validate routing and are excluded from this test run.
