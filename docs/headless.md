# Headless mode & third-party integration

NovaCode runs without a terminal UI in two shapes, both sharing the same agent
loop, tools, MCP servers and approval policy as the Textual TUI
(`novacode_cli/core/agent_loop.py`).

## One-shot runs: `nova -p "<prompt>"`

One prompt, one result, one process.

```
nova -p "Summarize the changes in src/" --output-format json
```

| Flag | Meaning |
|---|---|
| `--output-format text\|json\|stream-json` | `text` prints only the final answer; `json` one result object; `stream-json` NDJSON events then a result |
| `--max-turns N` | Cap observed main-agent turns (exit 2 when exceeded) |
| `--timeout S` | Deadline including agent startup (exit 124) |
| `--auto-approve` | Approve tool actions (a decision model may review; hard denies still apply) |
| `--deny-tools` | Fail closed: reject every approval |
| `--include-partial-messages` | `stream-json` also emits `text_delta`/`text_discard` |
| `--trust-workspace` | Approve the current folder explicitly |
| `--continue <session-id>` | Resume a previous (auto-saved) session |

### Slash commands

Any registry command works one-shot: `nova -p "/effort high" --output-format json`.

- The command runs through the shared registry — no agent turn, no cost.
- JSON result: `{"type":"result","subtype":"command","command":"/effort high",
  "ok":true,"result":"<command output>",...}`.
- Commands that hand off to the agent (e.g. `/goal <objective>` returns a
  kickoff prompt) are followed by a real agent turn; the final result is the
  agent's answer and carries `"command"` for provenance.
- Unknown commands return a structured error plus the surface hint
  (`nova commands --json`).

### Result object

```json
{"schema_version":1,"type":"result","subtype":"success","is_error":false,
 "result":"<final answer>","session_id":"…","num_turns":3,"duration_ms":…,
 "usage":{"input_tokens":…,"output_tokens":…,"cache_read_tokens":…,
          "cache_creation_tokens":…},
 "exit_code":0,"tool_call_count":5}
```

`subtype`: `success`, `command`, `error_max_turns`, `error_during_execution`,
`error_approval_required`, `error_timeout`, `error_cancelled`,
`error_incomplete_stream`, `error_during_initialization`.

### Exit codes

0 ok · 1 error · 2 max turns · 3 human input required · 124 timeout ·
130 cancelled · 141 output pipe closed.

Diagnostics go to **stderr**; stdout carries only machine output.

## Persistent sessions: `nova --mode pipe` (alias `--headless`)

A long-lived JSON-lines child that owns one session. Spawn it, read the
`ready` frame, exchange frames over stdin/stdout.

```jsonc
// stdin →
{"type":"prompt","id":"req-1","content":"List the failing tests"}
{"type":"prompt","id":"req-2","content":"Explain the first failure",
 "images":[{"base64_data":"…","format":"png"}]}
{"type":"command","id":"cmd-1","content":"/effort high"}
{"type":"approval","interrupt_id":"…","decision":"approve"}
{"type":"cancel"}
{"type":"shutdown"}
```

```jsonc
// stdout ← (every frame adds instance_id, session_id, sequence)
{"type":"ready","protocol":"nova.pipe","protocol_version":1,
 "capabilities":["prompt","command","cancel","approval","interrupt_reply",
                 "ping","shutdown","images"]}
{"type":"accepted","request_id":"req-1"}
{"type":"started","request_id":"req-1"}
{"type":"tool_call","tool":"execute","args":{…},"request_id":"req-1",…}
{"type":"tool_result","output":"…","request_id":"req-1",…}
{"type":"text","content":"…","request_id":"req-1",…}
{"type":"done","request_id":"req-1","status":"success","ok":true}
{"type":"command_result","request_id":"cmd-1","ok":true,"output":"…"}
{"type":"approval_required","request_id":"req-1","interrupt_id":"…",
 "kind":"tool","payload":{…}}        // answer with an approval frame
{"type":"stopped","exit_code":0}
```

Subagent observability (Ralph, Nora, Serena, `task` dispatches, Code Mode
workers) — upsert `subagent_task` rows by `task_id`, live `subagent_preview`
lines while a row runs, and `subagent_activity` dispatch/completion cards:

```jsonc
{"type":"subagent_task","request_id":"req-1","task_id":"ptc_task_ab12cd34",
 "status":"running","subagent_type":"research","label":"research: docling",
 "phase_id":"eval_1","phase_kind":"eval"}
{"type":"subagent_preview","request_id":"req-1","task_id":"ptc_task_ab12cd34",
 "kind":"text","text":"…"}
{"type":"subagent_task","request_id":"req-1","task_id":"ptc_task_ab12cd34",
 "status":"done","duration_ms":42000}   // same task_id: upsert, not append
```

Rules:

- `id` is caller-chosen (max 128 chars); reuse with the same content replays
  the `done`, with different content is a `request_id_conflict`.
- Commands (`command` frames) never create agent turns and are rejected while
  a prompt runs. A command that hands off to the agent returns `kickoff`;
  send it as a `prompt` when you want the turn.
- Images: up to 5 per prompt, `format` ∈ png/jpeg/jpg/webp/gif, ≤ 20 MB each.
- Unanswered `approval_required` frames time out (300 s default,
  `--approval-timeout`) into `done` status `approval_timeout`.
- `--input-format text` wraps each stdin line into a prompt frame.

## Discovering the surface

```
nova commands            # table: /name, route, description
nova commands --json     # machine-readable, incl. headless availability
```

`route` is where the behaviour lives: `passthrough` commands run headless;
`screen`/`client` commands are interactive-only and return structured errors
headless.

## Approvals

Fail-closed by default: policy-allowed actions run, everything needing a human
is rejected unless `--auto-approve` (one-shot) or answered over pipe
(`approval` frames). `--deny-tools` rejects everything.
