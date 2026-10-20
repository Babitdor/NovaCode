# Worktree tools

The current session enters a fresh Git worktree and exits back to its original
workspace. No process-wide chdir, branch deletion, worktree deletion, merge or
reset is performed. No dependencies are added. Setup uses temporary Git repos
in tests and the existing pytest, Ruff, mypy and coverage tools where available.
Spec approval: not obtained (autonomous run under the requested implementation).

Acceptance tests:

- Enter creates a unique branch from HEAD, switches file and shell roots, and
  leaves original uncommitted files behind. Two sessions with the same requested
  name receive different worktrees.
- Exit restores the original roots and preserves dirty files and new commits.
- Nested entry, exiting without entry, blank names, non-Git directories and
  repositories without a commit return errors without changing the workspace.
- Creation failure leaves the original workspace active.
- A workspace transition runs alone: mixed tool batches and concurrent tool
  execution reject transitions; a transition blocks other tool calls until done.
- Tools are always bound, registered on the main agent and not inherited by
  declarative subagents. Remote sandboxes return an unsupported-context error.
- The existing messaging tool remains always bound and compatible.

Workspace switching applies to the session's local file backend, shell tools,
project memory routes and middleware project roots. Tools with their own external
workspace (MCP servers, existing background jobs and remote agents) retain it.
Exit always keeps the worktree; removal remains an explicit user operation.
