# NovaCode 1.2.19

## Startup and MCP connections

- Remove live MCP discovery from agent construction. Nova loads matching
  cached schemas locally and starts connection discovery after the interface
  renders. Ordinary conversation turns can proceed while servers connect.
- Add **Retry** to `/mcp`, with connection-state labels and failure notifications.
  A retry bypasses the selected server's schema cache without restarting healthy
  peers or rebuilding the conversation.
- Keep newly discovered tools available through the MCP router, Code Mode,
  skill aliases, and explicitly authorized subagents.
- Bound connection attempts, share discovery between consumers, and cancel
  discovery and schema refresh when shutting down or reloading MCP configuration.
  Background failures no longer print over the terminal interface.

## CI

- Update agent-stream tests for mandatory human plan approval. The obsolete
  auto-approval test waited forever for a decision it never supplied, causing
  both Linux and Windows CI to time out.
- Run approval, background MCP, and provider-cache checks early in CI so failures
  are reported before the longer offline regression suite.

## Validation

Focused tests cover nonblocking startup and ordinary turns, late tool routing,
server-specific retries, OAuth compatibility, shutdown, reloads, skill aliases,
malformed cache recovery, and MCP dialog layouts. Full cold-launch speed has not
been benchmarked. No new runtime dependency is required.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.19
uv tool upgrade novacode-cli
```

Optional extras:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.19"
```
