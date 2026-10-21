# NovaCode v1.0.56

## Added

- Generic MCP discovery and execution with bounded results, server allowlists,
  and approval checks on the underlying tool.
- Separate cron create, delete, and list tools using the persistent scheduler.
- Optional context inspection and terminal panel capture tools behind feature gates.
- Regex and BM25 tool search variants alongside the existing hybrid search.
- Provider-aware strict tool schemas and raw argument validation, with documented
  capabilities and fallbacks for all 18 configured providers.
- Essential tool documentation covering discovery, feature gates, and strict mode.

## Changed

- Removed the GitHub trending, Hacker News, LinkedIn jobs, Reddit posts, and
  package info tools from the agent tool inventory.
- Made desktop sleep discoverable on demand instead of loading it by default.
- Retained shell and Bash for their distinct interpreter support.

## Fixed

- Applied shell approval policy to Bash commands.
- Hardened scheduler startup, persistence failures, and loading large job lists.
- Preserved MCP metadata through strict schema binding and rejected invalid raw
  argument types before tool execution.

## Verification

- Focused coverage includes local MCP execution, cron persistence, feature gates,
  search variants, strict validation, provider adapters, and subagent integration.
- Provider capabilities were checked against documentation and local adapters;
  live inference across all providers was not tested.
