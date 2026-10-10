"""Workspace re-anchoring for session continuation.

This module provides functionality to scan the current workspace state
and detect drift from saved sessions, ensuring the agent is grounded
in the current filesystem reality.
"""

import subprocess
from pathlib import Path
from typing import Any

def scan_workspace(project_root: Path | None = None) -> dict[str, Any]:
    """Scan current workspace state including git and filesystem.

    Args:
        project_root: Path to project root (git repo root)

    Returns:
        Dictionary containing workspace state:
        {
            "git_status": {...},
            "git_branch": "...",
            "git_head": "...",
            "modified_files": [...],
            "untracked_files": [...],
            "is_git_repo": bool,
            "working_directory": "...",
        }
    """
    state: dict[str, Any] = {
        "is_git_repo": False,
        # Use "/" as the virtual root path since FilesystemBackend with
        # virtual_mode=True maps "/" to the workspace root directory.
        # Windows absolute paths (e.g., B:\path) are rejected by
        # FilesystemMiddleware.validate_path, so we present the virtual
        # path "/" instead of the real filesystem path.
        "working_directory": "/",
    }

    if not project_root:
        return state

    # Check if it's a git repo
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return state

    state["is_git_repo"] = True

    # Get git status
    try:
        # Get current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if branch_result.returncode == 0:
            state["git_branch"] = branch_result.stdout.strip()

        # Get HEAD commit
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if head_result.returncode == 0:
            state["git_head"] = head_result.stdout.strip()[:12]

        # Get status --porcelain for modified/untracked files
        status_result = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if status_result.returncode == 0:
            modified = []
            untracked = []

            records = iter(status_result.stdout.split("\0"))
            for line in records:
                if not line:
                    continue

                status_code = line[:2]
                filename = line[3:]
                if "R" in status_code or "C" in status_code:
                    original = next(records, "")
                    filename = f"{original} -> {filename}"

                if status_code.startswith("??"):
                    untracked.append(filename)
                else:
                    modified.append(filename)

            state["modified_files"] = modified
            state["untracked_files"] = untracked
            state["has_uncommitted_changes"] = len(modified) > 0 or len(untracked) > 0

    except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
        # Git command failed, but that's okay
        pass

    return state


def detect_drift(
    saved_state: dict[str, Any] | None,
    current_state: dict[str, Any],
) -> list[str]:
    """Detect changes between saved and current workspace state.

    Args:
        saved_state: Previously saved workspace state
        current_state: Current workspace state from scan_workspace()

    Returns:
        List of drift warning messages
    """
    warnings: list[str] = []

    if not saved_state:
        return []  # No saved state to compare

    # Check if git repo status changed
    if saved_state.get("is_git_repo") != current_state.get("is_git_repo"):
        if current_state.get("is_git_repo"):
            warnings.append("Directory is now a git repository (was not before)")
        else:
            warnings.append("Directory is no longer a git repository")
        return warnings  # Major change, other checks don't make sense

    if not current_state.get("is_git_repo"):
        return warnings  # Not a git repo, no further checks

    # Check branch change
    saved_branch = saved_state.get("git_branch")
    current_branch = current_state.get("git_branch")
    if saved_branch and current_branch and saved_branch != current_branch:
        warnings.append(f"Git branch changed: {saved_branch} -> {current_branch}")

    # Check HEAD commit change
    saved_head = saved_state.get("git_head")
    current_head = current_state.get("git_head")
    if saved_head and current_head and saved_head != current_head:
        warnings.append(
            f"Git HEAD changed: {saved_head} -> {current_head}. New commits may have been added."
        )

    # Check for new uncommitted changes
    saved_had_changes = saved_state.get("has_uncommitted_changes", False)
    current_has_changes = current_state.get("has_uncommitted_changes", False)

    if not saved_had_changes and current_has_changes:
        modified_count = len(current_state.get("modified_files", []))
        untracked_count = len(current_state.get("untracked_files", []))
        warnings.append(
            f"New uncommitted changes detected: "
            f"{modified_count} modified, {untracked_count} untracked files"
        )
    elif saved_had_changes and not current_has_changes:
        warnings.append("Previously uncommitted changes have been committed or discarded")

    return warnings


def format_workspace_state_for_prompt(state: dict[str, Any]) -> str:
    """Format workspace state into a readable prompt section.

    Args:
        state: Workspace state from scan_workspace()

    Returns:
        Formatted markdown string for prompt injection
    """
    if not state.get("is_git_repo"):
        return """## Current Workspace State

Not a git repository.
Working directory: {working_directory}
""".format(working_directory=state.get("working_directory", "unknown"))

    lines = [
        "## Current Workspace State",
        "",
        f"**Git Branch:** `{state.get('git_branch', 'unknown')}`",
        f"**Git HEAD:** `{state.get('git_head', 'unknown')}`",
        f"**Working Directory:** `{state.get('working_directory', 'unknown')}`",
        "",
    ]

    # Add modified files if present
    modified = state.get("modified_files", [])
    if modified:
        lines.append("**Modified Files:**")
        for f in modified[:20]:  # Limit to first 20
            lines.append(f"- `{f}`")
        if len(modified) > 20:
            lines.append(f"- ... and {len(modified) - 20} more")
        lines.append("")

    # Add untracked files if present
    untracked = state.get("untracked_files", [])
    if untracked:
        lines.append("**Untracked Files:**")
        for f in untracked[:10]:  # Limit to first 10
            lines.append(f"- `{f}`")
        if len(untracked) > 10:
            lines.append(f"- ... and {len(untracked) - 10} more")
        lines.append("")

    if not modified and not untracked:
        lines.append("**Status:** Clean working directory")
        lines.append("")

    return "\n".join(lines)
