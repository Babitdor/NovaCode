# Code Mode

## Native tools

Code Mode now works without an MCP server. The existing `MCPCode` tool name is
retained for compatibility. Discover it with `search_tools`, then compose
ordinary Nova tools with the `tools` namespace:

```javascript
const catalog = await tools.list({offset: 0, limit: 20});
const schema = await tools.describe("read_file");
const files = await Promise.all(["/README.md", "/pyproject.toml"].map(file_path =>
  tools.call("read_file", {file_path})
));
return {catalog, schema, files};
```

Discovery includes the owning graph's registered ordinary tools, including
schemas deferred by tool search. It does not grant access to other sessions.
MCP tools remain available through `mcp.list/describe/call`, as shown below.
Both namespaces share one sandbox, call budget and concurrency limit.

Every native call validates its public arguments and traverses Nova's downstream
tool guards with a unique tool-call ID. Allowed edits and shell commands can run;
if a call needs interactive approval, make that tool call directly. Plan mode and
workspace brokers retain their existing restrictions, including blocking the
outer interpreter where it has no workspace authorization.

Recursive interpreters, delegation, user questions, mode/workspace transitions,
and tools declaring graph-state updates must be called directly. Text file reads
work; media reads that return graph updates require direct `read_file` calls.
An unexpected state-update result is reported as an error rather than discarded
as a successful result. Completed side effects are not transactional: errors,
timeouts and cancellation cannot guarantee rollback or stop synchronous work
already running in a host thread or external service. Do not automatically retry
writes or restart a partially completed script.

The composer toggle applies to both native and MCP calls. The existing `eval`
tool and its `task(...)` subagent orchestration remain separate and unchanged.

Run `uv run --no-sync python scripts/verify_mcp_code_mode.py` for the shared
runtime, native/MCP integration tests, coverage, lint, types and negative controls.
The original MCP-only contract and historical evidence follow.

## Original MCP acceptance contract

Implement `MCPCode` for the main session using the installed QuickJS runtime.
Each call runs an isolated JavaScript async function body. `mcp.list(options)`,
`mcp.describe(server, tool)` and `mcp.call(server, tool, arguments)` use only the
session's registered MCP inventory. Scripts can loop, branch, use `Promise.all`,
and return selected data without sending intermediate responses to the model.

Required behaviors:

1. Discovery includes only enabled tools and exposes original JSON schemas.
2. Calls validate the target schema without coercing argument types, even when
   provider strict mode is off. Unknown/disabled tools never execute.
3. Explicit denies on MCPCode, MCPTool or the target remain effective, including
   auto-approve sessions. Nested calls traverse downstream Nova tool middleware.
4. In manual-approval sessions, ask-tier calls fail closed with instructions to
   use the normal MCPTool approval path. This initial version does not suspend
   and replay JavaScript for approvals: replay could duplicate earlier writes.
5. Runtime credentials and arbitrary host filesystem/network APIs are not
   exposed to JavaScript. Authorized MCP servers retain their own capabilities.
6. Each nested execution has a unique tool-call ID and normal tool tracing.
   Intermediate values stay in the script; only its returned value is published.
7. Bound code size, CPU, memory, wall time, call count, concurrent calls and result
   size. Cancellation stops pending work; a subsequent call uses fresh state.
8. Server failures, schema errors, denied calls and timeouts are diagnosable.
   Partial external side effects cannot be rolled back by this feature.

Failure model: permission bypass, type coercion, disabled-tool routing, leaked
credentials, cross-session state, duplicate writes after retries, runaway code,
orphaned async work and unbounded payloads. Tests exercise each boundary, including
a real offline stdio MCP fixture. This is not a formal sandbox or security proof.

Setup: add two MCP modules, focused tests, documentation and CI entries. Reuse
QuickJS, jsonschema, LangChain and MCP dependencies already in the lockfile. Do
not install packages or change saved credentials/configuration. Keep existing
`eval` subagent dispatch unchanged. Bump version/changelog and push after checks,
following the user's standing release instruction.

Spec approval: not obtained separately (autonomous implementation of the approved
bridge proposal). Acceptance tests and the implementation share an author; this
limits independent confidence.

## Usage

Discover `MCPCode` with `search_tools`. Its `code` argument is an async function
body; use `return` to select what reaches the model:

```javascript
const catalog = await mcp.list({server_name: "database"});
const schema = await mcp.describe("database", "query");
return {catalog, schema};
```

```javascript
const batches = await Promise.all([1, 2, 3].map(id =>
  mcp.call("database", "read", {id})
));
return batches.map(batch => batch.summary);
```

Use exact server/tool names and argument schemas from discovery. Calls reject
with JavaScript errors; scripts can catch an error and choose whether to continue.
An execution with failed nested calls reports their count even if JavaScript
catches the errors. Avoid automatic retry of writes after a timeout or partial
failure. To approve an ask-tier call, use the normal namespaced tool or MCPTool;
approval of one ordinary call does not grant a reusable script-wide permission.

The runtime has no persistent JavaScript state or user-supplied imports. Tools
run with their original server credentials on Nova's side, not inside JavaScript.
Returned MCP data remains untrusted; filtering is not a prompt-injection defense.

## Limits and evidence

Per invocation: 40,000 code characters, a 64 MiB JavaScript heap, 2 seconds of
JavaScript execution, 60 seconds wall time, 30 seconds per nested tool call,
32 bridge calls (including discovery), four concurrent nested calls, 100,000
argument characters, 1,000,000 intermediate-result characters and 12,000 returned
result characters. Truncation is explicit. MCP servers and their response parsing
run outside the JavaScript heap limit. Standard JavaScript number precision
applies; request large integer identifiers and precise decimal values as strings.

Tracing follows Nova's normal configuration. Intermediate results stay out of the
model conversation, but normal tool traces may retain arguments and responses.
Manual ask-tier requests use the ordinary approval path; existing session rules
and the configured auto-approval model are respected. Plan mode blocks MCPCode;
use individual read-only MCP tools during planning. The synchronous graph API
returns an explicit unsupported-path error; Nova's shared agent loop is async.

Reproduce the focused checks with:

```powershell
.venv/Scripts/python.exe scripts/verify_mcp_code_mode.py
```

The verifier runs real QuickJS and a local stdio MCP fixture, offline scripted
model/graph tests, JSON property checks, branch coverage (85% enforced minimum),
lint, formatting, type checks and three negative controls. The controls remove
argument validation, the call budget and approval evaluation in separate child
processes; each must cause a test assertion failure. No source file is mutated.
CI runs the verifier and ordinary regressions on Windows and Linux.

### Implementation evidence

The final focused verifier passed 35 tests with 90% combined branch coverage,
full lint and format checks on the new runtime modules, type checks and all three
negative controls. JSON roundtrip properties cover 12 generated examples. The
tests verify filtered discovery, schema rejection with provider strict mode off,
explicit denies, manual and model-mediated approvals, existing session rules,
downstream guards, real stdio state reuse, parallelism, trace IDs and actual
outer results, memory/CPU/wall bounds, interruption, adapter-wrapped failures
and cross-call isolation.

The maintained CI regression file list passed locally with 796 tests, four
skips and 11 passing subtests. That final local run used `--timeout=60` to allow
for Windows UI startup overhead; individual Code Mode tests retain their own
30-second limit. The packaged version command reports v1.0.92. No dependency
versions changed; the lockfile change is the Nova package version only.

The all-repository `pytest tests/` attempt timed out in
`tests/test_agents_screen.py::test_a_pasted_system_prompt_is_used_instead_of_generating_one`.
A detached checkout of the unchanged v1.0.91 commit `741d197` also timed out on
that test. This is a known verification limit, not a claim that the entire suite
passed. The temporary checkout was removed after recording the baseline result.
No live provider or third-party MCP service was required for these checks;
real execution used the local test MCP process and QuickJS. No independent agent
verification or blanket prompt-injection/security guarantee is claimed.
