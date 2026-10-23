# NovaCode 1.0.92

## MCP Code Mode

- Add the discoverable `MCPCode` tool when MCP servers are configured. JavaScript scripts can list tool metadata, inspect JSON schemas, call multiple MCP tools, filter intermediate data and return a compact result.
- Support loops, branching, dependent calls and bounded parallel execution with `Promise.all`, using the existing QuickJS runtime. Existing `eval` subagent workflows remain unchanged.
- Validate every target's arguments and preserve configured tool inventories, policy denials, session rules, optional approval-model decisions and downstream tool middleware. Unresolved manual approvals use the ordinary MCPTool path rather than replaying a script.
- Keep script state isolated per call, preserve nested trace IDs, and report the outer script's actual result. Bound code, memory, execution time, call counts, concurrency and output; cancel pending async work on interruption.
- Add offline QuickJS and stdio MCP tests, JSON property checks and a verifier enforcing coverage, lint, types and three assertion-killed negative controls on Windows and Linux CI.

Usage and limits: `docs/mcp-code-mode.md`. This release adds no dependencies. External MCP side effects cannot be rolled back, and JavaScript number precision still applies.
