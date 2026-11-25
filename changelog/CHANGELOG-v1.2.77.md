# NovaCode 1.2.77

## Headless mode: full feature access + proper third-party responses

- **Slash commands in one-shot runs**: `nova -p "/effort high" --output-format json`
  dispatches through the shared registry — no agent turn, no cost. The result
  carries `subtype: "command"`, the command text and the captured output.
  Commands that hand off to the agent (e.g. `/goal <objective>`) are followed
  by a real agent turn, and the final result carries `command` provenance.
  Unknown commands return a structured error pointing at the surface catalog.
- **Pipe protocol: `command` frames.** New input frame
  `{"type":"command","id","content"}` answered by
  `{"type":"command_result","request_id","ok","output"}` (plus `kickoff` when
  the command hands off to the agent — the controller decides whether to send
  it as a prompt). Commands are rejected with `turn_running` while a prompt
  is active. `ready` capabilities now include `command`.
- **Images over pipe.** `prompt` frames accept
  `images:[{base64_data,format,placeholder}]` (≤ 5 images, png/jpeg/webp/gif,
  ≤ 20 MB each) — validated and normalized before the private worker protocol
  receives them. `ready` capabilities include `images`.
- **Surface discovery**: `nova commands [--json]` lists every slash command
  with its description, route and headless availability.
- **Result enrichment**: one-shot results carry `tool_call_count` (observed
  main-agent tool calls) in JSON/stream-json output.
- New `docs/headless.md` documents the whole third-party contract: flags,
  frame catalogue, command dispatch, approvals, exit codes, examples.
