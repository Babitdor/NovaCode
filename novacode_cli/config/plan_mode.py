# ruff: noqa: INP001 -- Match the existing config namespace.
"""Single source of truth for plan mode tool restrictions.

All enforcement modules (HITL approval, middleware, prompt templates)
import from this module to prevent silent drift between independently
maintained block lists.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

# Bound by the shared loop; the live session flag also covers enter_plan_mode
# and owner-approved exit within the same request, including nested tool calls.
active_plan_session: ContextVar[Any] = ContextVar("active_plan_session", default=None)

# ── Blocked tools ──────────────────────────────────────────────────────
# These tools are completely disallowed during plan mode.  They modify
# state (shell, git, server, test runner). Planning checklists are allowed:
# write_todos changes tracking state without implementing the plan.
BLOCKED_TOOLS: frozenset[str] = frozenset(
    {
        # Shell execution
        "shell",
        "execute_bash",
        "execute",
        "bash",
        "execute_code",
        "apply_patch",
        "delete_file",
        "move_file",
        "write_memory",
        "task",  # A general implementation subagent cannot bypass planning restrictions.
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
    }
)

# ── Restricted write tools ─────────────────────────────────────────────
# These tools are allowed in plan mode ONLY when the target path is
# inside `.nova/plans/`.
RESTRICTED_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
    }
)

# ── Human-readable summary for prompt templates ────────────────────────
BLOCKED_TOOLS_DISPLAY = "`" + "` `".join(sorted(BLOCKED_TOOLS)) + "`"
RESTRICTED_WRITE_TOOLS_DISPLAY = "`" + "` `".join(sorted(RESTRICTED_WRITE_TOOLS)) + "`"

# Reviewed Serena investigation operations. Memory writes, symbolic edits, onboarding,
# project activation and command execution are deliberately excluded.
SERENA_READ_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "find_file",
        "search_for_pattern",
        "find_symbol",
        "get_symbols_overview",
        "find_referencing_symbols",
        "list_memories",
        "read_memory",
        "get_current_config",
        "check_onboarding_performed",
        "initial_instructions",
        "think_about_collected_information",
        "think_about_task_adherence",
        "think_about_whether_you_are_done",
    }
)


def is_plan_path(path: str, workspace_root: Path | None = None) -> bool:
    """Allow only contained plan/tracking paths, rejecting traversal and lookalikes."""
    if not path:
        return False
    normalized = path.replace("\\", "/")
    if ".." in normalized.split("/"):
        return False
    root = (workspace_root or Path.cwd()).resolve()
    # Absolute virtual paths map to the workspace, as in FilesystemBackend.
    target = root / normalized.lstrip("/") if normalized.startswith("/.nova/") else Path(path)
    if not target.is_absolute():
        target = root / target
    try:
        resolved = target.resolve()
        return any(
            directory.is_relative_to(root) and resolved.is_relative_to(directory)
            for name in ("plans", "ralph")
            for directory in [(root / ".nova" / name).resolve()]
        )
    except (OSError, ValueError):
        return False


def _mcp_identity(
    name: str,
    args: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[str, str]:
    """Prefer host-recorded identity over display-name prefixes or model arguments."""
    server = str(metadata.get("nova_mcp_server", ""))
    if server:
        return server, str(metadata.get("nova_mcp_tool") or name.removeprefix(server + "_"))
    if name == "MCPTool":
        return str(args.get("server_name", "")), str(args.get("tool_name", ""))
    if name.startswith("mcp__"):
        server, _, raw = name.removeprefix("mcp__").partition("__")
        return server, raw
    if name.lower().startswith("serena_"):
        return "serena", name[len("serena_") :]
    return "", ""


def plan_tool_rejection(  # noqa: PLR0911 -- Reject each disallowed capability at the boundary.
    name: str,
    args: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
    workspace_root: Path | None = None,
) -> str | None:
    """Classify real tool identity before approval; unknown MCP capabilities fail closed."""
    metadata = metadata or {}
    if name == "MCPTool" and args.get("action", "list") in {"list", "describe"}:
        return None
    server, raw = _mcp_identity(name, args, metadata)
    if server:
        if raw in BLOCKED_TOOLS or raw in RESTRICTED_WRITE_TOOLS:
            return f"[Plan Mode] MCP tool `{name}` cannot execute or modify files while planning."
        if "serena" in server.lower():
            allowed = raw in SERENA_READ_TOOLS
        else:
            allowed = metadata.get("readOnlyHint") is True
        if allowed and metadata.get("destructiveHint") is not True:
            return None
        return f"[Plan Mode] MCP tool `{name}` is not an approved read-only operation."
    if name == "MCPTool" or name.startswith("mcp__"):
        return "[Plan Mode] MCP calls require a known server and read-only tool."
    if name in BLOCKED_TOOLS:
        return (
            f"[Plan Mode] `{name}` is blocked during planning. Use read-only investigation tools."
        )
    if name in RESTRICTED_WRITE_TOOLS and not is_plan_path(
        str(args.get("path") or args.get("file_path") or ""),
        workspace_root,
    ):
        return "[Plan Mode] writes are only allowed inside .nova/plans/ or .nova/ralph/."
    return None
