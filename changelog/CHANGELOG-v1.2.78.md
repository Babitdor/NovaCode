# NovaCode 1.2.78

## Headless: the Claude-Code / Codex best-practice set

Everything the big coding CLIs expose for scripted and CI use, now in Nova's
headless mode:

- **Schema-constrained output**: `--output-schema <json-or-file>` (alias
  `--json-schema`). The model is instructed up front to answer with
  schema-valid JSON; the answer is validated at the end and the result
  carries `structured_output` when it matches (omitted otherwise, like
  Claude Code).
- **Per-tool allowlist**: `--allowed-tools "read_file,execute"` (aliases:
  `Read`, `Edit`, `Write`, `Bash`, `Glob`, `Grep` — so Claude-Code-style
  invocations work). Policy denies stay authoritative; the allowlist is an
  explicit operator opt-in for this run.
- **Budget cap + cost estimate**: `--max-budget-usd` stops the run when the
  client-side estimate exceeds the amount (`error_budget_exceeded`), and the
  result carries `total_cost_usd` when the model's price is known.
- **Enriched init event**: stream-json `system/init` now includes `tools`
  and `mcp_servers` with statuses plus `mcp_server_errors` — a CI gate can
  fail when a configured MCP server did not load.
- **Bare mode**: `--bare` skips plugin, MCP-preset, proactive and memory
  discovery for fast, deterministic scripted runs.
- **Background-task wait-at-exit**: a one-shot turn that started background
  tasks waits for them (10-minute default, `NOVA_HEADLESS_BG_WAIT_SECONDS`)
  and takes one final turn summarizing their outcome.
- **Subagent correlation**: `tool_use`/`tool_result` events carry
  `parent_tool_use_id` (the owning task id) in stream-json and pipe frames.
- **Image input**: `--image <path>` (repeatable, 5 max, 20 MB each) attaches
  images to a one-shot prompt.
- **10 MiB stdin cap** (was 1 MiB), and SIGTERM in an unattended run exits
  143 after killing running shell process trees.
