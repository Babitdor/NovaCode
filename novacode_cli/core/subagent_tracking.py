"""Subagent tracking and display logic for execution.

This module handles:
- Tracking active subagents and their activities
- Formatting subagent completion banners
- Managing subagent nesting and indentation

Shared between the TUI, headless mode, and server mode.
"""

import time
from typing import TYPE_CHECKING

from novacode_cli.config.config import console

if TYPE_CHECKING:
    pass


# Type aliases for clarity
SubagentStack = list[tuple[str, str]]  # [(tool_call_id, subagent_type), ...]
SubagentActivity = dict  # {"name": str, "calls": list, "files_read": list, ...}
ActiveSubagents = dict[str, tuple[str, str, float]]  # tool_call_id -> (type, desc, start_time)
_MAX_ACTIVITY_PATHS = 10


class SubagentTracker:
    """Tracks subagent execution state and displays completion banners."""

    def __init__(self):
        # Track task tool calls for subagent banner display: {tool_call_id: (subagent_type, description, start_time)}
        self.active_subagents: ActiveSubagents = {}

        # Track subagent nesting for indentation: [(tool_call_id, subagent_type), ...]
        self.subagent_stack: SubagentStack = []

        # Track subagent activity by LangGraph namespace (lazily created on first tool call)
        self.subagent_activity_by_ns: dict[tuple, SubagentActivity] = {}

        # Map LangGraph namespace -> tool_call_id (populated when we see the first ToolMessage from a subagent)
        self.lg_ns_to_tool_call_id: dict[tuple, str] = {}

        # Guard: prevent double dispatch when tool_call_chunk arrives multiple times
        self.dispatched_subagents: set[str] = set()

        # Track when each tool call was first displayed (for elapsed-time display)
        self.tool_call_start_times: dict[str, float] = {}

        # Deferred subagent completion banners — printed together just before main-agent synthesis
        self.pending_completions: list[dict] = []

    def get_indent(self) -> str:
        """Get indentation based on current subagent nesting depth."""
        return "  " * len(self.subagent_stack)

    def dispatch_subagent(
        self,
        tool_call_id: str,
        subagent_type: str,
        description: str,
    ) -> bool:
        """Register a new subagent dispatch. Returns True if newly dispatched."""
        if tool_call_id and tool_call_id not in self.dispatched_subagents:
            self.dispatched_subagents.add(tool_call_id)
            self.active_subagents[tool_call_id] = (
                subagent_type,
                description,
                time.time(),  # start_time
            )
            self.subagent_stack.append((tool_call_id, subagent_type))
            return True
        return False

    def complete_subagent(self, tool_call_id: str) -> tuple[str, str, float] | None:
        """Mark a subagent as complete and return its info."""
        if tool_call_id in self.active_subagents:
            info = self.active_subagents.pop(tool_call_id)
            # Pop from stack
            for i, (tid, _) in enumerate(self.subagent_stack):
                if tid == tool_call_id:
                    self.subagent_stack.pop(i)
                    break
            return info
        return None

    def get_or_create_activity(
        self,
        namespace: tuple,
        subagent_type: str | None,
    ) -> SubagentActivity:
        """Get or create activity tracking for a LangGraph namespace."""
        activity = self.subagent_activity_by_ns.get(namespace)
        if activity is None:
            activity = {
                "name": subagent_type or "subagent",
                "files_read": [],
                "files_read_count": 0,
                "files_written": [],
                "files_written_count": 0,
                "error_count": 0,
                "categories": {},
            }
            self.subagent_activity_by_ns[namespace] = activity
        return activity

    def record_tool_call(
        self,
        namespace: tuple,
        subagent_type: str | None,
        tool_name: str,
        tool_args: dict,
        tool_categories: dict[str, str],
    ) -> None:
        """Record a tool call in subagent activity tracking."""
        activity = self.get_or_create_activity(namespace, subagent_type)
        category = tool_categories.get(tool_name, "other")
        activity["categories"][category] = activity["categories"].get(category, 0) + 1

        # Track specific paths for file operations
        if category == "files_read":
            path = tool_args.get("path") or tool_args.get("file_path") or "?"
            activity["files_read_count"] += 1
            if len(activity["files_read"]) < _MAX_ACTIVITY_PATHS:
                activity["files_read"].append(path)
        elif category == "files_written":
            path = tool_args.get("path") or tool_args.get("file_path") or "?"
            activity["files_written_count"] += 1
            if len(activity["files_written"]) < _MAX_ACTIVITY_PATHS:
                activity["files_written"].append(path)

    def record_error(
        self,
        namespace: tuple,
        tool_name: str,
    ) -> None:
        """Record an error in subagent activity tracking."""
        activity = self.subagent_activity_by_ns.get(namespace)
        if activity:
            activity["error_count"] = activity.get("error_count", 0) + 1

    def claim_namespace_for_tool_call(
        self,
        namespace: tuple,
        tool_call_id: str,
    ) -> SubagentActivity | None:
        """Claim an unclaimed namespace for a tool call completion.

        Returns the activity dict if found, None otherwise.
        """
        if namespace not in self.lg_ns_to_tool_call_id:
            activity = self.subagent_activity_by_ns.get(namespace)
            if activity:
                self.lg_ns_to_tool_call_id[namespace] = tool_call_id
                self.subagent_activity_by_ns.pop(namespace, None)
                return activity
        return None

    def add_pending_completion(
        self,
        status_icon: str,
        subagent_type: str,
        duration_str: str,
        condensed: str | None,
        subagent_color: str,
    ) -> None:
        """Add a deferred completion banner to be printed later."""
        self.pending_completions.append({
            "status_icon": status_icon,
            "subagent_type": subagent_type,
            "duration_str": duration_str,
            "condensed": condensed,
            "subagent_color": subagent_color,
        })

    def flush_completions(self, spinner_active: bool, status) -> tuple[bool, bool]:
        """Print all deferred subagent completion banners as one compact block.

        Returns:
            Tuple of (spinner_was_active, spinner_should_restart)
        """
        if not self.pending_completions:
            return spinner_active, False

        if spinner_active:
            status.stop()
            spinner_active = False

        console.print()
        for comp in self.pending_completions:
            icon = comp["status_icon"]
            name = comp["subagent_type"]
            dur = comp["duration_str"]
            cond = comp["condensed"]
            color = comp["subagent_color"]
            if cond:
                console.print(f"  {icon} {name}{dur}: {cond}", style=color)
            else:
                console.print(f"  {icon} {name}{dur}", style=color)
        self.pending_completions.clear()
        console.print()

        return spinner_active, True

    def get_remaining_count(self) -> int:
        """Get count of remaining active subagents."""
        return len(self.active_subagents)

    def get_done_count(self) -> int:
        """Get count of completed subagents (pending display)."""
        return len(self.pending_completions)

    def clear(self) -> None:
        """Reset all tracking state for a new iteration."""
        self.active_subagents.clear()
        self.subagent_stack.clear()
        self.subagent_activity_by_ns.clear()
        self.lg_ns_to_tool_call_id.clear()
        self.dispatched_subagents.clear()
        self.tool_call_start_times.clear()
        self.pending_completions.clear()


def format_duration(elapsed: float) -> str:
    """Format elapsed time as a duration string."""
    if elapsed >= 60:
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        return f" ({mins}m {secs}s)"
    else:
        return f" ({int(elapsed)}s)"


def get_status_icon(success: bool) -> str:
    """Get status icon for completion."""
    return "✓" if success else "✗"
