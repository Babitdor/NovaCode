# Essential tools

These tools are discoverable through `search_tools`; their schemas are loaded
when needed rather than added to every model request.

## NotebookEditTool

Edits an existing local `.ipynb` notebook in nbformat 4 without running code.
Use `read_file` first to inspect cells, then discover `NotebookEditTool`.

- `file_path`: relative, virtual-root (`/analysis.ipynb`), or absolute path inside
  the local workspace. Remote sandbox notebooks are not supported.
- `edit_mode="replace"`: select `cell_id` or zero-based `cell_index`, and provide
  `new_source`. Omit `cell_type` to retain the existing type.
- `edit_mode="insert"`: provide `cell_type` (`code`, `markdown`, or `raw`) and
  `new_source`. Insert before `cell_index`, after `cell_id`, or append when neither
  selector is given. Exactly one selector may be supplied.
- `edit_mode="delete"`: select the existing cell; omit source and type changes.

Notebook metadata and untouched cells are preserved. Changed code cells clear
stale outputs and execution counts. New cells receive IDs when the format
requires them. Edits use a temporary file and atomic replacement; invalid inputs
and failed replacements leave the original intact. Concurrent calls within one
process are serialized. The tool follows file-path approval rules and is blocked
in plan mode. No Jupyter kernel or extra Python dependency is required.

## GlobTool / GrepTool

Nova exposes these capabilities as the existing `glob` and `grep` tools. Imported
agent definitions can use `GlobTool`/`GrepTool` as aliases. This avoids registering
duplicate tool schemas.

Local file searches use `bfs` for traversal when available, preserving glob
filtering, vendor-directory pruning and newest-first results. Content search
tries `ugrep`, then ripgrep, then the Python fallback. These optional executables
are discovered on PATH; Nova does not install them automatically. Native failures
and timeouts fall back to the next engine. Searches invoke argument arrays without
a shell. Existing remote sandbox search behavior is unchanged.

Grep remains literal by default, with smart-case matching; the backend also
supports explicit regular expressions. The Python fallback matches basename globs
at any depth and respects single-file search paths.

## MCPTool

Available when MCP servers are configured. It uses Nova's existing discovery,
server allowlists, schema cache and persistent stdio connections.

- `action="list"`: list server names and raw tool names. Filter with
  `server_name`; paginate with `offset` and `limit` (maximum 50).
- `action="describe", server_name="db", tool_name="query"`: inspect the actual
  JSON schema, including required parameters.
- `action="call", server_name="db", tool_name="query", arguments={...}`:
  execute through the same downstream middleware as a direct MCP call.
  Approval policy evaluates the underlying namespaced tool and arguments.
  Unknown, disabled and filtered tools cannot be called.

For large structured results, `result_path=["rows", 0, "name"]` selects data
locally before returning it. `max_output_chars` bounds the returned text
(default 12,000; maximum 50,000). The response reports truncation. Selection
does not execute arbitrary code. Existing `search_tools` discovery and direct
namespaced calls continue to work.

## CronCreateTool, CronDeleteTool, CronListTool

Available in CLI sessions and backed by the same scheduler as `/cron`.
Creation takes `cron_expr` and `task`; deletion takes the exact `job_id` returned
by creation or listing. Creation and deletion use Nova's approval gate and are
blocked in plan mode. Listing is read-only.

Expressions have five fields and use the machine's local time. Jobs are saved
to Nova's durable store and resume in interactive sessions. An open Nova session
must consume the scheduled tasks; this is not an operating-system scheduler.
Headless creation returns an explicit unsupported-session error. A storage
failure surfaces as an error and leaves the in-memory job unchanged.

## CtxInspectTool and TerminalCaptureTool

Disabled by default. Set `CONTEXT_COLLAPSE=1` or `TERMINAL_PANEL=1` before
starting Nova. `NOVA_CONTEXT_COLLAPSE` and `NOVA_TERMINAL_PANEL` are equivalent
prefixed gates and take precedence. False values disable the corresponding tool.

`CtxInspectTool()` reports conversation counts, token estimates and content-free
metrics from the most recent assembled request. It does not collapse or change
history. Counts are estimates, not provider billing figures.

`TerminalCaptureTool(panel_id="task_42", tail_lines=100, max_chars=12000)`
captures retained text from a Nova-managed background terminal task panel.
Get IDs from `list_background_tasks`. It strips ANSI styling and reports
truncation. It supports running and completed tasks, and cannot capture another
application's terminal or an operating-system screenshot.

The standalone agent server includes enabled inspection tools and MCP routing.
Cron tools require the interactive session's scheduler queue.

## Tool search variants

Nova exposes two local callable implementations using the requested variant
identifiers. They work across providers; they are not Anthropic server-side
`type` declarations:

- `tool_search_tool_regex_20251119(query, limit=5, offset=0)`: case-insensitive
  Python regex syntax over names, descriptions and argument documentation.
  Patterns are limited to 200 characters and run under an execution deadline.
- `tool_search_tool_bm25_20251119(query, limit=5, offset=0)`: independent BM25
  ranking over the same catalog. Queries are limited to 500 characters and do
  not load embedding models or download weights.

Both variants load the matched schemas for the next model call, including after
checkpoint restoration. Nova retains its local pagination limit of 50 per page.
`search_tools` remains a compatible hybrid-search entry point for existing
prompts and saved sessions. Both new variants reject string numbers for integer
arguments even on providers without native constrained decoding.

See [Anthropic's native tool search contract](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
for the corresponding provider-side tool types and their different API limits.

## Strict tool use

Set `strict_tools` in Nova's configuration, or use `NOVA_STRICT_TOOLS`:

- `auto` (default): add native constraints only for audited routes and compatible
  schemas. All registered function calls are validated before execution without
  coercing values such as `"2"` to `2`.
- `on`: require a verified/requestable native mode and compatible bound schemas;
  otherwise fail clearly before sending the model request. For routed providers,
  this requests strict mode from the upstream; Nova cannot independently verify
  the upstream's constrained sampler.
- `off`: retain the previous provider settings and tool validation behavior.

Open dictionaries and unsupported schema constraints are not silently removed.
In `auto`, these tools retain their original definitions and receive execution
validation rather than a false native-strict claim. Closed object definitions
require existing properties on the wire, retain defaults in descriptions and do
not invent nullable values for non-nullable parameters. Registered execution
tools and their Python defaults are unchanged. Schema compliance cannot establish
authorization, business correctness, successful execution or correct tool results.

The same middleware applies to main agents, in-process declarative subagents,
and packaged background agents. Provider routing is evaluated per request so
model switches use the new provider's contract.

### Provider audit

Audit date: 2026-10-21. All 18 Nova provider IDs are covered by a registry test.
This is a documentation and adapter audit with offline request tests, not paid
live-inference certification. A provider's structured final responses do not by
themselves establish strict tool-input support.

| Provider ID | Nova behavior | Evidence / limitation |
| --- | --- | --- |
| `openai` | Native `function.strict` for compatible schemas | [Function calling](https://developers.openai.com/api/docs/guides/function-calling). Custom endpoints are not assumed equivalent. |
| `anthropic` | Native `strict` with `input_schema` on recognized supported Claude families | [Strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use). Legacy model names are excluded. |
| `azure` | Native function flag on recognized Azure endpoints; parallel calls disabled | [Azure requirements](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs). Unknown deployment aliases need explicit `on`. |
| `google` | `VALIDATED` function-calling mode for compatible toolsets | [Function modes](https://ai.google.dev/gemini-api/docs/function-calling). Leaves natural-language responses possible. Open maps, explicit choices and incompatible schemas are not silently changed. |
| `bedrock` | Native `toolSpec.strict` only for recognized Claude families and an SDK exposing that field | [Converse contract](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html). Model/region support still applies. |
| `cerebras` | Native function flag on the audited GPT-OSS 120B, Qwen 3.8 27B and Gemma 4 31B IDs | [Tool calling](https://inference-docs.cerebras.ai/capabilities/tool-use). Other IDs remain local-only pending confirmation. |
| `xai` | No extra flag; retain its documented implicit strict behavior | [Structured outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs). Execution validation remains enabled. |
| `openrouter` | Local validation in `auto`; explicit `on` sends flags and `provider.require_parameters=true` | [LangChain tool example](https://openrouter.ai/blog/tutorials/langchain-chatopenrouter-setup/), [routing limitations](https://openrouter.ai/docs/guides/features/structured-outputs). Upstream enforcement varies. |
| `huggingface` | Local validation in `auto`; explicit `on` requests the documented strict flag | [Function calling](https://huggingface.co/docs/inference-providers/guides/function-calling). The selected inference backend must support it. |
| `ollama` | Local validation; no unverified strict flag | [Tool calling](https://docs.ollama.com/capabilities/tool-calling) documents calls; JSON response formatting is a separate feature. |
| `mistral` | Local validation; native strict tool contract not confirmed for Nova's chat route | [Function calling](https://docs.mistral.ai/studio/conversations/function-calling). |
| `groq` | Local validation; no assumption that response-format strictness also covers tool calls | [Local tools](https://console.groq.com/docs/tool-use/local-tool-calling), [structured outputs](https://console.groq.com/docs/structured-outputs). |
| `nvidia` | Local validation; no automatic hosted-NIM strict guarantee | [NIM APIs](https://docs.api.nvidia.com/nim/reference/llm-apis). Self-hosted Dynamo guided decoding is a different deployment. |
| `minimax` | Local validation on Nova's Anthropic-compatible route | [SDK compatibility](https://platform.minimax.io/docs/api-reference/text-anthropic-api) does not establish native Claude strict semantics. |
| `kimi_coding` | Local validation; native strict sampling not confirmed for the coding endpoint | [Kimi Code API errors](https://www.kimi.com/code/docs/en/kimi-code/error-reference.html). |
| `commandcode` | Local validation across Nova's selected gateway routes | [Provider API](https://commandcode.ai/docs/provider) forwards tool arrays; forwarding alone is not an upstream grammar guarantee. |
| `opencode` | Local validation; no automatic strict claim for heterogeneous gateway models | [Gateway models](https://opencode.ai/docs/zen/). |
| `opencode_zen` | Local validation; same gateway-specific uncertainty | [Gateway models](https://opencode.ai/docs/zen/). |

`jsonschema`, `referencing` and `regex` are now explicit dependencies (all were
already present in the lockfile) for non-coercing validation, reference errors
and time-bounded matching.
