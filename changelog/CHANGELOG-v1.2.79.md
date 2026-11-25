# NovaCode 1.2.79

## Fixed: MCP custom-server modal crash

Adding a custom stdio MCP server (e.g. `npx -y @netlify/mcp`) crashed with
`TypeError: MCPServerConfig.validate_command() missing 1 required positional
argument: 'info'` — the modal called the pydantic field validator directly
without a ValidationInfo. The checks now live in
`MCPServerConfig.check_command`, callable directly, and the validator
delegates to it.

(Also carries v1.2.78's headless best-practice set: `--output-schema`,
`--allowed-tools`, `--max-budget-usd`, `--bare`, `--image`, enriched init
event, background-task wait-at-exit, subagent correlation, 10 MiB stdin,
SIGTERM handling — see the v1.2.78 changelog.)
