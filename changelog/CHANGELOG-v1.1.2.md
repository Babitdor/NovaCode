# NovaCode 1.1.2

## Nova Prime · Deep Work

- Add opt-in Deep Work with `/prime on`, `/prime off`, `/prime status`,
  `/prime report`, and the `--prime` / `--no-prime` launch flags.
- Track six ordered checkpoints: define, investigate, implement, verify,
  challenge and deliver. Delivery records distinguish agent assessments from
  observed tool results and explicitly identify unverified work.
- Save Prime preferences and the last report with session metadata. Interrupted
  work remains paused; clearing a conversation removes its report, and late
  results cannot repopulate a cleared or superseded request.
- Preserve the selected model, existing approvals and execution limits.

## Composer controls

- Add Prime and Plan toggles with mouse and keyboard support, preserving drafts
  and returning focus to the composer without starting agent work.
- Show a theme-aware Prime frame and a blue **Plan Mode · Read only** frame,
  highlighted toggles and mode indicators. Bash and approval-warning colors
  retain precedence; controls wrap on narrow terminals.
- Keep Prime and Plan on/off changes silent in the transcript. Explicit status
  and report commands continue to display their results.

## Plan-mode guardrails

- Enforce planning restrictions before synchronous and asynchronous tool
  execution, including the main agent entering planning mid-conversation.
- Block Serena edits, memory writes, command execution and unknown operations,
  including calls routed through MCPTool and Code Mode. Reviewed read-only
  investigation remains available; auto-approval cannot override the guard.
- Preserve authoritative MCP server/tool identity. Other MCP tools must declare
  read-only behavior without a destructive hint to execute during planning.
- Reject traversal, directory lookalikes and escaped targets in the permitted
  `.nova/plans/` and `.nova/ralph/` write paths.

## CI fixes

- Test the shared planning policy directly instead of patching a removed
  approval-module constant; cover both manual and automatic approval settings.
- Bring reasoning headers into view before mouse hit-testing after folding or
  expanding long traces, preserving all reopening and full-text assertions.
- Include Prime state, composer controls and MCP planning restrictions in the
  offline CI regression matrix and focused source lint checks.
