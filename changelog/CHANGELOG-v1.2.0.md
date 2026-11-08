# NovaCode 1.2.0

## Async-agent diagnostics

- Report failed background runs' checkpoint errors instead of a generic failure,
  including provider permission and expired-model errors. Match diagnostics to
  the exact run, redact credentials, bound output, and keep child transcripts private.
- Reconcile nonce-owned factory workers even when OS birth timestamps differ
  from the launch clock, preserving PID birth checks before termination.

## Startup fixes

- Remove provider token-count API calls from startup and use local approximations
  for unsupported counters without caching them as native counts.
- Defer user async-agent validation until the first background delegation,
  preserving validation before server launch and shutdown/cancellation handling.
- Skip unrelated external-server probes and bound MCP discovery without waiting
  for slow worker cleanup.

## Project onboarding

- Add `/onboard` to build a persistent workspace code graph and onboarding guide
  with the existing Graphify parsers. Suggest the command on the home screen and
  include it in command completion and help.
- Add `/onboard open`, `/onboard refresh`, `/onboard tour`,
  `/onboard ask <question>`, and `/onboard impact <symbol or path>`.
- Add an interactive dashboard with Nova's Tokyo Night palette and shared block
  wordmark, a searchable file and symbol explorer, a zoomable dependency map,
  a symbol inspector, guided tours, and change-impact tracing.
- Render the onboarding guide as formatted Markdown with raw HTML disabled.
  Prepare Q&A commands for the existing Nova conversation and its normal tool
  permissions.
- Adapt the dashboard for desktop and mobile, with keyboard navigation,
  visible focus, and reduced-motion support. Keep dashboard assets local.
- Show an inline spinner and elapsed time while onboarding runs, and remove
  the indicator on completion, failure, or cancellation.
- Detect changed, new, and deleted inputs with content fingerprints. Preserve
  the saved analysis when parsing fails, times out, or is cancelled.
- Scale parsing deadlines with input size from two to ten minutes. Normalize
  Windows short paths before extraction so graph nodes and edges are retained.
- Respect Git ignore rules, reject linked storage paths, and disable repository
  filesystem-monitor hooks during file discovery. Keep workspace, input-size,
  graph-size, and Plan Mode checks in place.

## Code Mode and skill visibility

- Make `eval` the primary Code Mode tool, with `MCPCode` compatibility, variables
  reused across snippets within a conversation, and guarded access to native
  tools, MCP tools, and configured subagents.
- Queue simultaneous interpreter snippets within a conversation instead of
  rejecting them. Keep separate conversations independent.
- Pause live interpreter execution for tool approvals and resume the same
  script without replaying completed actions.
- Normalize in-workspace Windows paths for nested file operations and pin the
  verified QuickJS integration version to prevent startup import failures.
- Show successful skill reads inline and guide the agent to announce which
  skill it is reading and why.
- Keep native filesystem page text and structured tool data separate from
  display formatting and repeat warnings, so reads can safely feed exact edits.
  Preserve output limits, repeat blocks and independent parallel-read state.

## Workflow and remote chat

- Replace Prime's earlier continuation loop with a supervised software factory.
- Add native Telegram approval buttons and workspace image previews.
- Render complete compaction summaries as Markdown in the compact card.
- Improve team ownership handoff and retire superseded in-process workers
  during updates.
