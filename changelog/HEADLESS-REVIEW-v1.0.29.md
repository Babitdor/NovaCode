# Headless pipe review — v1.0.29

## Interface

`nova -p` remains a one-shot UTF-8 prompt over stdin, terminated by EOF. It supports text, JSON, and JSON-lines output. It does not expose the private session-worker protocol as a public interactive RPC interface.

Nova additionally provides a separate persistent public `--mode pipe` / `--headless`
interface for remote apps. See [the protocol guide](../docs/PIPE-MODE.md).

New controls: `--timeout`, `--include-partial-messages` for stream-json, and explicit `--trust-workspace` (folder-only). JSON records carry schema version 1; result records include exit_code.

## Review findings and fixes

- Headless runs silently enabled auto-approve and persisted recursive directory trust. The CLI now requires explicit permission for each; unresolved human approvals reject and report code 3.
- The old approval tests called an async resolver without awaiting it. Fixed them and added explicit unresolved-verdict coverage.
- Raw descriptor writes assumed a complete write. The formatter now retries interrupted writes and completes partial UTF-8 writes.
- Python/native/child diagnostic writes could contaminate stdout. Descriptor redirection keeps the result pipe separate; tested with actual Windows subprocesses.
- Windows closed pipes return CRT EINVAL. The formatter maps that error to BrokenPipeError only for pipe descriptors; a real closed-reader test found and verified this fix.
- Added deadlines for asynchronous startup and execution, bounded source cleanup and autosave, structured startup failures, cancellation codes, and incomplete-stream detection.
- Provider network timeouts remain execution errors, distinct from an expired Nova deadline.
- Turn counting excludes subagent output and advances across tool-only rounds. The final text excludes subagent prose. This counter measures observed events rather than exact API calls.
- Once a result is flushed, cleanup interruptions preserve its exit code and cannot emit a second contradictory result. Failed diagnostic streams cannot hide the result.
- Prompt input is bounded to 1 MiB and validates UTF-8; incompatible interactive CLI options are rejected before startup.

## Evidence

- Final focused regression run: **107 passed**, covering headless, actual CLI/subprocess pipes, closed Windows readers, trust/approval handling, deadlines/cancellation, session-worker compatibility, and model-role resolution.
- Broader integration run: **252 passed, 1 failed** from a Windows HTTP socket reset in the existing browser authorization test. Both browser authorization cases passed on rerun.
- Ruff F checks pass for headless modules and their tests. `git diff --check` passes. Main retains pre-existing unused dependency-probe imports.
- Agent responses in pipe tests are deterministic fakes; these checks do not validate a live model provider. Existing full-suite collection is limited by the environment's missing hypothesis dependency.

## Practical limits

- The deadline is cooperative; blocking native code cannot be preempted by asyncio. External controllers should impose a process deadline when they need a strict wall-clock bound. Cleanup has a bounded grace period.
- --deny-tools rejects approvals, not all tools. Existing policy allows remain authoritative. Headless mode does not guarantee a read-only run.
- Tool turn limits apply to observed event rounds and cannot undo a tool already executed before its event was emitted.
- Session persistence is best effort; the result is emitted before the bounded autosave.

## Model-role investigation

Dynamic selects the default for custom agents discovered from agent.md files and dispatched in the current process; frontmatter model overrides Dynamic, which otherwise follows Subagents then Main.

Async selects the default for agent-server runs. Custom async:true agents can be used through either route. Async selection follows the saved Async role, explicit server environment, then Main. The existing server middleware can fall back to the graph default when it cannot build a per-run model; the picker reflects the requested model rather than proving the runtime override succeeded.
