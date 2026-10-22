# NovaCode v1.0.80

## Fixes

- Require explicit parent approval for teammate file-tool writes that resolve
  outside the worker's project, even when auto-approve is enabled. Configured
  path allow rules still permit approved destinations; explicit denies win.
  Resolve mounted paths and symlinks before approval and recheck the destination
  before executing. Worktree workers use their own directory.
- Identify each teammate's assigned directory in its system instructions and
  prevent task text or remembered project paths from implying write permission.
- Keep the two main-agent base templates packaged with Nova. Global prompt
  history and activated prompt experiments cannot replace them. Global agent.md
  remains persistent memory rather than the source of the base system prompt.

## Improvements

- Improve the default Nova coding instructions using Codex.md as inspiration:
  task-sized planning, meaningful progress updates, project output scope,
  preserving user changes, approval continuity, focused verification and honest
  completion reports, adapted to Nova's tools.
- Add workspace and packaged-prompt regression tests to CI on both platforms.

## Limits

- File-tool approval checks are not an OS sandbox for shell commands, Python,
  plugins or external MCP tools. Prose output selections are not silently
  converted into broad folder grants; external file writes use parent approval
  or configured path rules. A small filesystem race remains between the final
  path check and execution.
