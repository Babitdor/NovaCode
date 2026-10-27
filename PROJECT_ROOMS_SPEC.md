# Project rooms acceptance contract

The supplied implementation plan authorizes this implementation. Existing session
tabs, mailboxes, logins, working files, and CLI permission controls remain intact.

- Rooms default to #general plus Nova, Claude, and Codex DMs. Opening rooms never
  invokes a model. Mentions select agents without consuming file mentions.
- Canonical Git worktrees share a SQLite workspace; non-Git discussion works.
  SQLite transactions serialize delivery, conversation claims and task budgets.
- Participants have bound identities and room access. Shared text never confers
  owner authority. DMs never enter another participant's context or note export.
- Discussion is read-only. Owner-approved tasks record goal, participants,
  operations, base revision, worktree and warnings before execution. Maximum two
  running turns project-wide; six turns per task including handoffs.
- External CLI JSON streams provide native session IDs, tool details and usage.
  Missing installation/login and denied approvals produce actionable blockers.
- Handoffs preserve scope and worktree and require structured evidence. Agent
  tools can publish/read context and hand off; cannot approve/start/integrate.
- Cancellation kills and reaps workers. Dead runtime claims recover as paused,
  never replay. Closing Nova stops its workers; other Nova sessions retain theirs.
- Integration requires a separate owner action, a clean canonical checkout and
  unchanged base. Worktrees survive errors, pauses and integration.
- Sidebar navigation preserves drafts/unread state; transcript scrolling remains
  independent. Narrow terminals can collapse navigation. Session tabs stay intact.

Failure model: conflicting runtimes (transactional claim tests); forged identity
or DM access (bound-tool tests); runaway handoffs (budget tests); restart/replay
(dead-runtime recovery tests); data loss (dirty/base integration refusal tests);
subprocess deadlock/cancellation (fake stream tests); UI draft loss (pilot tests).

Verification uses offline fake CLIs and temporary Git repositories. Live CLI
smoke tests remain opt-in and require the owner's installed/logged-in tools.
