# Invoking custom agents

Create persistent definitions as `<project>/.nova/agents/<name>.md` or
`~/.nova/agents/<name>.md`. Project definitions override global namesakes.
Existing `<name>/agent.md` directories remain supported.

## Invocation modes

| Definition | Synchronous tool | Background tool | TUI `@name` default |
| --- | --- | --- | --- |
| No `async` flag, or `async: false` | `task` | None | Synchronous delegation |
| `async: true` | `task` | `start_async_task` | Synchronous delegation |
| `async: only` | None | `start_async_task` | Background delegation |

Both tools accept `subagent_type=<name>` and a focused `description`. Background
graph IDs are lowercase. Their public name does not need an `-async` suffix.
Duplicate registration is rejected within each tool namespace; the same name
may have one synchronous and one background registration.

`@reviewer review this diff` instructs the main agent to delegate; it does not
switch the conversation to another agent. For multiple mentions, Nova builds
the route for each agent separately, preserving the requested order and result
dependencies. Background-only mentions use `start_async_task`. An unavailable
server for this workspace produces an explicit message rather than a
synchronous fallback. A background task ID does not mean the work is complete.

Background execution requires the configured Nova/LangGraph agent server and
the correct workspace. `/agents` already notifies the owned server when its
background definitions change. Externally managed servers remain responsible
for loading their own updated definitions; client roster refresh cannot update
a running external server's graph registry.

## Loading changed definitions

Root TUI sessions and session workers compare a content signature before each
new turn. Create, edit, delete and project-override changes recompile the roster
off the event loop. Same-size rewrites are detected; no file metadata cache is
used for this check. The current model, workspace, tools, store, checkpointer,
session ID and steering instructions are preserved. A roster refresh does not
emit a model-switch hook. Unchanged definitions do not trigger a rebuild.

Refresh occurs between serialized turns, not inside an active tool invocation.
A definition created during a turn becomes callable on the next turn. Rebuild
errors abort that turn instead of silently executing stale permissions or tools.
Explicitly bound team workers retain their spawn-time capability snapshot;
new definitions do not widen a running teammate's access.

Headless launches build a fresh roster at startup. Explicit `@` preprocessing
currently belongs to the main TUI dispatcher; this change does not introduce
new slash commands or promise identical mention handling in every remote client.

## Verification

`tests/test_agent_invocation.py` exercises actual compiled `task` and
`start_async_task` tools with scripted models and a fake remote client. It checks
same-name dual invocation, project selection, mixed TUI mentions, unavailable
background execution, graph refresh in the TUI/session worker, checkpoint
retention, same-size changes and protected teammate snapshots. The roster
registration, resilience, session, checkpoint and team suites cover compatibility.
These tests make no provider calls and do not establish live-server or real-model
task quality.

Retained verification (2026-10-20): **275 passed in 79.75 seconds**, including
the invocation, registration, resilience, agent management, session, checkpoint
and team regressions. Ruff and formatting pass for the new invocation module
and its tests; scoped mypy passes for the new module. Diff whitespace checks
pass. The full repository suite was not rerun for this change.

Checking the existing runtime module with mypy still reports two optional
`assistant_id` errors. Both reproduce on unchanged revision
`36e5c8369c4a3c58dadf48139042f7e4cd718218`; they were left unchanged. Raw logs,
JUnit XML and the test manifest remain local under
`%TEMP%/nova-invocation-retained-kw6rtnz9/`. No dependencies, provider calls,
personal configuration changes or push were required.
