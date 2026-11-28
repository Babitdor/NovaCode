# v1.2.93 — Tool-error reductions from session analysis

**Released 2026-11-27** · PyPI `novacode-cli==1.2.93` · tag `v1.2.93`

Analysis of a real session export (148 dispatched calls, 14 failures ≈ 9.5%)
identified four mechanically preventable failure classes, two hint gaps, and
an export defect. All are addressed.

## Repair layer

- **Out-of-range numbers clamp to the schema maximum.** `tools:list` with
  `limit: 60` against a cap of 50 now clamps and says so
  (`value-clamped`), instead of rejecting the call.
- **Missing required properties fill from their schema defaults.** Playwright's
  `browser_take_screenshot` declares `scale` required with a default — the
  server's own choice is filled in (`default-filled`).
- **Enum violations reset to the schema default.** `write_memory` called with
  `memory_type: 'topic'` (a guessed value; the tool tolerates it natively)
  resets to the default instead of failing validation.
- **Unknown-property errors name the real parameters.** Instead of dumping the
  schema blob: `… was unexpected (did you mean 'glob'? allowed: …)`.
- **Unknown native tools get a did-you-mean.** `grep_search` → "did you mean
  'grep'?" instead of a generic "use tools.list()".

## MCP routing

- **Double-prefixed tool names resolve.** `playwright_browser_navigate` on
  server `playwright` (alias `browser_navigate`) now strips the server prefix
  and resolves; the dispatch outcome records the correction
  (`step.resolved`, logged `mcp_route_repaired`).
- **Unresolvable names get a did-you-mean hint** with the server's known tool
  aliases, replacing "use mcp.list().".

## Hints

- Timeout failures now point at the concrete remedy: retry `execute` with
  `background=true` and poll `get_task_status`, and the shell's local-limit
  rejection says the same.

## Exports

- **Tool results are written once.** The markdown and JSON exports wrote a
  ToolMessage body twice (as the node's bare text and again as the Result
  block) — doubling every Code Mode result in the file and skewing analyses.
