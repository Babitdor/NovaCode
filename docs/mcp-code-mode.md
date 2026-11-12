# Code Mode

Code Mode makes `eval` the agent's primary control surface. The composer toggle
applies to existing conversations immediately. When enabled, ordinary native and
MCP schemas stay in the host registry; the model composes operations in JavaScript.
Tool searches, including provider-native search and its fallback, do not restore
ordinary schemas to the model inventory.

Separate `eval` and `MCPCode` calls in one conversation queue for the same
interpreter instead of colliding. Use `Promise.all` inside one snippet for bounded
parallel host operations. Waiting for the interpreter does not consume a snippet's
execution deadline. Other conversations keep their own interpreter.

Successful skill reads show an inline “Reading skill: …” notice, including reads
inside `eval`. Nova is also instructed to announce the skill and why it applies
before reading it. Discovery and failed reads do not announce skill use.

Nova pins `langchain-quickjs` to the tested `0.3.2` release because the guarded
bridge uses its private interpreter and snapshot APIs. Fresh installations must
honor the package dependency constraint; upgrading this library requires testing
and adapting the bridge first.

Interaction, planning, workspace transitions, background-task controls and media
fallbacks remain directly available. Plan Mode and Browser Mode retain their
restricted direct-tool paths. Disabling Code Mode restores ordinary tool exposure
and removes both interpreter entry points and their instructions.

The direct `read_file` schema is advertised as a media fallback. Read source code,
Markdown and other text through `tools.call('read_file', ...)` inside `eval`, using
exact workspace virtual paths from discovery (for example `/pkg/graph.py`). On a
missing file, discover the path with `glob` or `ls` before retrying.

Paginated text reads from Nova's filesystem backend return the requested source
text to JavaScript, including its trailing-newline state. Display gutters,
pagination headers and loop-warning notes stay out of that string, so it can be
used directly as `edit_file.old_string`. Literal headers or warning-like text
inside the file are preserved. Empty-file reminders, media and third-party
backend result formats retain their existing behavior.

```javascript
var source = await tools.call("read_file", {file_path: "/guide.md", offset: 0, limit: 60});
await tools.call("edit_file", {
  file_path: "/guide.md",
  old_string: source,
  new_string: source.replace("# Old heading", "# New heading")
});
```

Read data still passes through approvals and all tool middleware. If a page was
truncated or transformed, Nova rejects the raw read and requests a smaller page;
it does not expose text that the existing display limits removed. Parallel calls
keep independent read data, and cancellation discards late results. Repeat
warnings remain visible in tool output and repeat blocks still stop execution.
Argument-repair notices use the same separation, including when diagnostics stack.

## JavaScript API

`eval` accepts JavaScript statements and expressions, including top-level `await`.
The final expression selects the result sent to the parent model. `console.log`,
`console.warn` and `console.error` provide bounded explicit output. Intermediate
tool values and child transcripts stay outside parent model history; normal
execution traces and subagent progress remain available separately.

```javascript
var native = await tools.list({offset: 0, limit: 20});
var schema = await tools.describe("grep");
var servers = await mcp.list({limit: 20});
var remoteSchema = await mcp.describe("database", "read");
({native, schema, servers, remoteSchema});
```

Use exact names and arguments from discovery. Native calls use
`tools.call(tool_name, arguments)`; MCP calls use
`mcp.call(server_name, tool_name, arguments)`. Results retain structured values.
Loops, filtering and `Promise.all` can combine calls before selecting a small result.
`tools.list` accepts `offset` and `limit`; filter its `tools` array in JavaScript
instead of passing `query`. QuickJS provides no Node.js `Buffer`, `process` or
`require`. Use the file tools for text writes. On Windows, invoke PowerShell
explicitly when using its cmdlets through `execute`.

```javascript
var batches = await Promise.all([1, 2, 3].map(id =>
  mcp.call("database", "read", {id})
));
var selected = batches.filter(row => row.needsReview);
var reviews = await Promise.all(selected.map(row => task({
  description: "Review this record: " + JSON.stringify(row),
  subagentType: "reviewer"
})));
reviews;
```

`task({description, subagentType, responseSchema?, label?})` uses the configured
subagent roster and Nova's tool middleware. `tools.describe("task")` exposes the
roster. An optional JSON response schema requests structured child output.
Supported file-state updates propagate without copying child conversation history;
unsupported state updates and graph routing mutations fail explicitly. Explicit
`@agent` routing selects `eval` for synchronous delegation; background-only agents
retain their background-task route.

`MCPCode` remains a compatibility entry to the same runtime. It accepts an async
function body with `return` and retains its JSON response envelope: `result`,
`truncated`, `calls` and `failed_calls`. `eval` uses the same envelope, with the
interpreter's displayed final-expression result. Explicit console output is
returned alongside that value. Failed bridge calls are counted even when caught.
State-loss warnings also accompany failed snippets. Timeout errors recommend a
smaller batch and inspecting completed calls before retrying; no side effect is
rolled back. See [the issue review](github-issue-review.md#5--code-mode-reliability)
for reproduced reporting defects and the remaining validation limits.
Filesystem tools' textual `Error:` results also fail the nested call; a script
cannot silently proceed past a failed edit and report a successful update.

Keep large source batches in variables and select small passages to return.
Truncated results include a recovery notice: inspect smaller slices of those
variables before making source claims, without replaying completed operations.
For Markdown writes, arrays of quoted lines joined with `"\n"` avoid nesting
Markdown backticks inside JavaScript template literals. Syntax failures include
quoting guidance; Nova does not rewrite or automatically retry the script.

## State and permissions

Variables persist between snippets and user turns in the same conversation and
owning agent. Start a new conversation or reset to get fresh state. Nova uses the
installed `langchain-quickjs` thread snapshots and existing checkpoints. Snapshots
do not promise recovery of every JavaScript value after a restart. A failed or
oversized snapshot or failed restore is reported in the next successful result's
`state_warning`; it does not imply variables survived.

The interpreter exposes guarded host proxies, not raw tool objects. Each nested
call has a distinct identity, validates its original schema without coercion, and
passes through Nova's policy, planning, workspace, plugin and execution middleware.
Denies remain effective under auto-approve. JavaScript has no direct host filesystem,
network, imports or credentials. Tool results remain untrusted evidence.

When a nested action requires human approval, the shared loop presents its normal
tool-approval view and suspends the live script. Approval, rejection or edited
arguments apply only to that action. Execution resumes at that point without
restarting the snippet or repeating earlier writes. Edited arguments are validated
again. Missing handlers, cancellation, changed permissions and stale conversations
fail closed. Reset cancels pending approvals; late decisions cannot resume them.

Completed side effects are nontransactional. Errors, timeouts and cancellation
cannot roll them back or guarantee that synchronous host work stops. Never
automatically retry a partially completed script.

## Limits and verification

Defaults per snippet: 40,000 code characters, a 64 MiB JavaScript heap, 2 seconds
of JavaScript execution, 60 seconds of active wall time, 30 seconds per ordinary
nested call, 32 bridge calls including discovery, four concurrent calls, 100,000
argument characters, 1,000,000 intermediate-result characters and 12,000 selected
result characters. Console buffering is separately bounded to 12,000 characters.
Human approval waits do not consume execution deadlines. Delegation uses existing
subagent execution limits. Truncation is explicit; standard JavaScript number
precision applies. MCP host parsing runs outside the JavaScript heap limit.

Nova's normal execution is asynchronous; synchronous interpreter calls fail closed.
Run the offline verifier, including the real stdio MCP fixture, integration tests,
coverage, lint, types, regressions and assertion-killed negative controls:

```powershell
.venv/Scripts/python.exe scripts/verify_mcp_code_mode.py
```

The existing CI runs this verifier on Windows and Linux. No new dependencies,
Python execution runtime or orchestration service are required.
