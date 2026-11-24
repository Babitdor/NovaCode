# NovaCode 1.2.61

## Serena Mode: Nova acts through the Serena MCP toolset

`/serena` (on/off/status) activates a mode where Nova's built-in filesystem,
shell, web and knowledge tools step aside and the model works exclusively
through MCP tools — Serena's language-server-backed semantic tools plus any
other configured MCP servers.

- **Gated on a live Serena connection.** Activating checks the shared MCP
  runtime for a connected `serena` server (case-insensitive). Without one,
  the command refuses with guidance: no server configured → open `/mcp`,
  where Serena ships as a ready-made preset; connection failed → inspect or
  retry there; still connecting → wait and re-run.
- **Serena-shaped system prompt.** A mode block re-anchors the workflow on
  Serena's discipline: navigate symbols before files (find_symbol,
  get_symbols_overview, find_referencing_symbols), edit at the symbol level,
  pattern search and shell for the rest, use Serena's thinking checkpoints
  when bound, treat project memories as first-class, and call
  serena_initial_instructions once per coding task when present.
- **Request-local tool filter, no agent rebuild.** Modeled on Browser Mode:
  only MCP tool schemas (plus the MCPTool router and Code Mode's eval
  surface) are exposed, and execution fail-closed rejects anything else even
  if a model guesses the call. `task` stays bound so subagent delegation —
  and Code Mode's `task()` bridge — keep working.
- **Works with Code Mode.** Snippets reach MCP via
  `mcp.list()/mcp.describe()/mcp.call(...)`; the Serena prompt says so,
  because the `tools.call` namespace intentionally does not carry MCP tools.
  ToolSearch binds the whole MCP inventory directly in this mode — nothing
  hides behind `search_tools`.
- Mutually exclusive with plan and browser modes, like they are with each
  other; the mode badge, prompt styling, and the composer modes line
  ("serena: mcp only") all reflect the state. The flag rides session state
  and resets with the conversation.
