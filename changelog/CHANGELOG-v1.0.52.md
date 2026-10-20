# NovaCode v1.0.52

## Added

- Experimental agent teams, disabled by default: persistent in-process teammates,
  fork/worktree workers, a configured container adapter, durable scoped inboxes,
  versioned task claims, approval forwarding and separately gated swarm scheduling.
- Editable built-in specialists shipped as flat agent definition files and seeded
  once into the global agents directory without overwriting user definitions.
- Automatic memory-wiki consolidation behind time, session-count and ownership
  gates, with a restricted background dream agent and `/dream` controls.
- Request-local snip/collapse strategies before model compaction, recoverable
  offloaded results, and context accounting based on schemas actually sent.

## Fixed

- Project agent definitions override global namesakes before execution-mode
  filtering, consistently across lookup, synchronous/background rosters and HTTP
  listings. New definitions use `<project>/.nova/agents/<name>.md` or
  `~/.nova/agents/<name>.md`; existing directory definitions remain supported.
- Root TUI sessions and session workers refresh changed definitions before their
  next turn while retaining the model, checkpoint, workspace and policy settings.
- Background-only mentions route to `start_async_task`; `async: true` retains
  both synchronous and background invocation under the same name.
- Context displays distinguish estimated category shares from provider input
  usage, account for deferred tools and identify MCP schemas by server.
- Prompt guidance names background tools only when their server is usable.

## Verification and limits

- The retained invocation/session/team regression passed 275 checks. Pre-push
  checks across 41 test files, using isolated groups and reruns, finished with
  520 passing cases, one skip and three UI failures reproduced on unchanged
  baseline: todo collapse, long-list scrolling and the responsive-layout mock.
  A timing-sensitive stream check passed on rerun; a bounded UI batch's remaining
  cases were run separately. This is not a green full-suite result.
- A fresh staged-source wheel includes all 14 built-in agent files and the new
  modules, and excludes the removed specialist modules and review captures.
- Team/container behavior remains experimental. Live Docker/provider execution,
  real-model task quality and resumable reconnect are unverified. Recovery is
  cleanup-only; shared file-read caching remains deferred.
- Earlier broad-suite failures/timeouts and two baseline runtime typing errors
  are documented in the agent-team and invocation evidence. No new dependencies
  or paid model calls were required.
