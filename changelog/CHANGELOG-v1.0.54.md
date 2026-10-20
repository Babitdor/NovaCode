# NovaCode v1.0.54

## Added

- Essential `SendMessage(recipient, content)` tool for plain-text messages between
  connected project tabs, using existing queued delivery and linked replies.
  `list_sessions` is also always available for recipient discovery.
- Essential `EnterWorktreeTool(name)` and `ExitWorktreeTool()` for moving the
  current local agent's file and shell tools into a fresh Git worktree and back.
  Exit keeps the worktree, branch, commits and uncommitted changes.
- Workspace transition guards for parallel tool calls and cancellation, normal
  approval handling, and plan-mode restrictions.
- Usage documentation and real Git, shell, agent-graph and mutation checks.

## Verification and limits

- 80 focused worktree, tool-discovery and messaging regression tests passed;
  all three targeted behavioral mutants were detected.
- Three release metadata, version banner and real CLI version checks passed.
- Messaging connects tabs within one NovaCode TUI instance. Independently
  launched terminal instances and standalone headless runs are not connected.
- Worktree switching requires a local Git repository with a commit. External
  MCP servers, existing jobs and remote agents retain their own workspaces.
  Active workspace switches are not restored after resume or agent rebuild.
- Full-suite collection remains blocked by the installed deepagents API mismatch
  (`create_sub_agent` is absent). Five upstream startup-version scan tests also
  fail against missing installed deepagents internals. New files pass Ruff;
  no dependencies were added.
