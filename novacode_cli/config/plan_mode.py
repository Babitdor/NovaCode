"""Single source of truth for plan mode tool restrictions.

All enforcement modules (HITL approval, middleware, prompt templates)
import from this module to prevent silent drift between independently
maintained block lists.
"""

from __future__ import annotations

# ── Blocked tools ──────────────────────────────────────────────────────
# These tools are completely disallowed during plan mode.  They modify
# state (shell, git, server, test runner). Planning checklists are allowed:
# write_todos changes tracking state without implementing the plan.
BLOCKED_TOOLS: frozenset[str] = frozenset({
    # Shell execution
    "shell",
    "execute_bash",
    "execute",
    "MCPCode",  # Scripts can compose writes; use individual read-only MCP calls while planning.
    # Server management
    "start_dev_server",
    "stop_server",
    # Test execution
    "run_tests",
    # Git operations (modifies repo)
    "git_branch",
    "git_stash",
    "EnterWorktreeTool",
    "ExitWorktreeTool",
    "memory_wiki",
    "CronCreateTool",
    "CronDeleteTool",
    "NotebookEditTool",
    "computer_action",  # Desktop input can change files and submit external operations.
})

# ── Restricted write tools ─────────────────────────────────────────────
# These tools are allowed in plan mode ONLY when the target path is
# inside `.nova/plans/`.
RESTRICTED_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
})

# ── Human-readable summary for prompt templates ────────────────────────
BLOCKED_TOOLS_DISPLAY = "`" + "` `".join(sorted(BLOCKED_TOOLS)) + "`"
RESTRICTED_WRITE_TOOLS_DISPLAY = "`" + "` `".join(sorted(RESTRICTED_WRITE_TOOLS)) + "`"
