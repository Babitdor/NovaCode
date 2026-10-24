# Code Mode: native tool composition

Implementation plan, 2026-10-24. User authorized planning and execution together.
Spec approval: not obtained separately (autonomous run).

## Scope and acceptance

1. Existing `MCPCode` calls and `mcp.list/describe/call` remain compatible.
   Keep the tool name and its policy key; do not register a second interpreter.
2. Add `tools.list({offset?, limit?})`, `tools.describe(name)`, and
   `tools.call(name, arguments)` for ordinary tools registered on the owning
   graph. Works without any configured MCP server. Catalogs are request-local,
   include deferred tools, and never leak another session's tools.
3. List/describe expose original public schemas only, without injected runtime
   credentials. Unknown tools, invalid arguments, recursive interpreters,
   user-question, delegation, workspace/mode transitions, and declared graph
   state mutation tools fail before execution. Ordinary text `read_file` is
   supported; media reads returning graph commands require a direct call.
   Undeclared unexpected graph commands fail visibly and are never treated as
   ordinary successful data. MCP tools remain under the existing `mcp.*` API.
4. Every nested native execution uses the owning ToolNode and all downstream
   guards, strict validation, callbacks, workspace restrictions, and a unique
   injected tool-call ID. Policy denial wins even in auto mode. An action
   needing interactive approval fails before execution and directs the model
   to call that tool directly. Scripts do not pause/resume or replay actions.
5. Existing sandbox limits, concurrency/call budgets, cancellation, bounded
   results, failure accounting, no host filesystem/network globals, and
   existing MCP permission checks remain in force for both namespaces.
6. Toggle off hides/rejects the interpreter for native and MCP scripts. Composer
   help describes native tools, and Code Mode is discoverable without MCP.

## Failure model

- Authorization bypass: graph integration tests for deny, ask, auto mode,
  downstream workspace guard, and MCP routing isolation.
- Cross-session capability leak: interleaved requests on separate graphs;
  inventory comes from ToolRuntime, not middleware instance caches.
- Replayed/partial writes: approval failure must not run target or replay prior
  actions; no transactions or rollback promised after completed tool calls.
- State loss/recursion: excluded controls and Command-return annotations checked
  before invocation; unexpected Command result errors explicitly.
- Invalid schemas/runtime injection: public schema validation before invoking;
  hostile injected runtime argument rejected.
- Unbounded work: existing runtime regression tests plus native budget,
  concurrency and cancellation integration tests.

## Setup and verification

- Reuse Python, QuickJS, LangChain/LangGraph, pytest, Hypothesis, ruff, coverage
  and mypy already installed. No new runtime or development dependencies.
- Move generic ownership into `agents/code_mode.py` and `agents/code_runtime.py`,
  retain compatibility imports in `mcp/`, register once in `core_agent.py`.
- Add graph-level tests first and observe failures before implementation.
- Focused suites: native/MCP Code Mode, toggle, tool search, attribution,
  deepagents compatibility and relevant security tests. Baseline existing
  failures before edits. Run broader tests when integration evidence warrants.
- Persist focused verification/mutation entry point in `scripts/`; record
  final results, source hash, tool versions and omitted layers in
  `CODE_MODE_EVIDENCE.md`. No commits, release, push, or dependency changes.
- Use scoped lint/format and compare diagnostics with baseline for legacy files;
  do not reformat unrelated existing code. Record type/coverage limitations.

## Non-goals

Interactive approvals inside a script; automatic replay/resume; graph control,
delegation and recursive interpreter dispatch; arbitrary state reducers;
automatic retries of writes; persistent JS state; transactional rollback.
