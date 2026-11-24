"""Helpers for tracking file operations and computing diffs for CLI display.

This module provides utilities for tracking, previewing, and displaying
file operations in the CLI:

Key Components:
- FileOpTracker: Tracks file operations across a session
- ApprovalPreview: Previews changes for user approval
- build_approval_preview(): Generate approval UI for file operations
- render_file_operation(): Display file operations with rich formatting

Features:
- Track write_file, edit_file, and other file operations
- Compute unified diffs for edit operations
- Generate approval previews with context and error handling
- Render file operations with color coding and formatting
- Support for both local and sandbox backends

The FileOpTracker maintains a list of operations performed during a session,
allowing for review and approval of destructive operations. The approval
preview shows:
- File path and operation type
- Diff preview for edits
- Context (lines before/after)
- Error messages if validation fails

Used by execution.py for tool approval and UI rendering.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

logger = logging.getLogger(__name__)

from novacode_cli.config.config import settings

if TYPE_CHECKING:
    from deepagents.backends.protocol import BACKEND_TYPES

FileOpStatus = Literal["pending", "success", "error"]

# Maximum number of completed file-operation records retained per session.
# The completed list is write-only (no display consumer reads it), but each
# record can hold full file contents, so an unbounded list leaks memory across
# a long session. Older records are trimmed and their heavy fields dropped.
_MAX_COMPLETED_RECORDS = 200


@dataclass
class ApprovalPreview:
    """Data used to render HITL previews."""

    title: str
    details: list[str]
    diff: str | None = None
    diff_title: str | None = None
    error: str | None = None


def _safe_read(path: Path) -> str | None:
    """Read file content, returning None on failure.

    Tries multiple encodings to handle files with non-UTF-8 content:
    1. UTF-8 (most common)
    2. Latin-1 (can decode any byte sequence)
    3. System default encoding (Windows charmap, etc.)
    """
    # Try UTF-8 first (most common for source code)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pass
    except OSError as e:
        logger.warning(f"_safe_read OSError for {path}: {e}")
        return None

    # Try latin-1 (can decode any byte sequence without error)
    try:
        return path.read_text(encoding="latin-1")
    except OSError as e:
        logger.warning(f"_safe_read OSError for {path}: {e}")
        return None
    except Exception as e:
        logger.warning(f"_safe_read unexpected error for {path}: {type(e).__name__}: {e}")
        return None


def _count_lines(text: str) -> int:
    """Count lines in text, treating empty strings as zero lines.

    For formatted read output with line numbers, this counts the actual
    source lines (excluding continuation lines like 5.1, 5.2 for long lines).
    """
    if not text:
        return 0

    lines = text.splitlines()
    if not lines:
        return 0

    # Check if this is formatted output with line numbers (e.g., "     1\tcontent")
    # Formatted lines have format: "{line_num:{width}d}\t{content}" or "{line_num.decimal}\t{content}"
    first_line = lines[0] if lines else ""

    # Detect formatted output: line number followed by tab
    import re

    formatted_pattern = re.compile(r"^\s*\d+(\.\d+)?\t")
    is_formatted = bool(formatted_pattern.match(first_line))

    if is_formatted:
        # Count unique line numbers (excluding continuation markers like 5.1, 5.2)
        unique_lines = set()
        for line in lines:
            match = formatted_pattern.match(line)
            if match:
                # Extract the base line number (before any decimal)
                line_marker = match.group(0).strip().rstrip("\t")
                if "." in line_marker:
                    # This is a continuation line (e.g., "5.1"), skip it
                    continue
                try:
                    unique_lines.add(int(line_marker))
                except ValueError:
                    pass
        return len(unique_lines) if unique_lines else len(lines)

    return len(lines)


def compute_unified_diff(
    before: str,
    after: str,
    display_path: str,
    *,
    max_lines: int | None = 800,
    context_lines: int = 3,
) -> str | None:
    """Compute a unified diff between before and after content.

    Args:
        before: Original content
        after: New content
        display_path: Path for display in diff headers
        max_lines: Maximum number of diff lines (None for unlimited)
        context_lines: Number of context lines around changes (default 3)

    Returns:
        Unified diff string or None if no changes
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{display_path} (before)",
            tofile=f"{display_path} (after)",
            lineterm="",
            n=context_lines,
        )
    )
    if not diff_lines:
        return None
    if max_lines is not None and len(diff_lines) > max_lines:
        truncated = diff_lines[: max_lines - 1]
        truncated.append("...")
        return "\n".join(truncated)
    return "\n".join(diff_lines)


@dataclass
class FileOpMetrics:
    """Line and byte level metrics for a file operation."""

    lines_read: int = 0
    start_line: int | None = None
    end_line: int | None = None
    lines_written: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    bytes_written: int = 0


@dataclass
class FileOperationRecord:
    """Track a single filesystem tool call."""

    tool_name: str
    display_path: str
    physical_path: Path | None
    tool_call_id: str | None
    args: dict[str, Any] = field(default_factory=dict)
    status: FileOpStatus = "pending"
    error: str | None = None
    metrics: FileOpMetrics = field(default_factory=FileOpMetrics)
    diff: str | None = None
    before_content: str | None = None
    after_content: str | None = None
    read_output: str | None = None
    read_image: str | None = None
    """Virtual path of an image this read returned (deepagents hands images
    back as base64 content blocks plus the source path) — the TUI mounts an
    inspectable card and remote bridges send a photo preview for it."""
    hitl_approved: bool = False
    parent_call_id: str | None = None
    """The call this operation happened inside, when it was not a tool call of
    its own — a Code Mode snippet's nested edit. The eval call is the only card
    the transcript has, so the diff belongs under that one."""


#: File tools that change content: the ones worth reporting before/after and
#: diffing. One definition, used by the tracker and by Code Mode's nested dispatch.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "Write", "Edit"})


def resolve_physical_path(path_str: str | None, assistant_id: str | None) -> Path | None:
    """Convert a virtual/relative path to a physical filesystem path.

    Handles the following virtual path prefixes:
    - /memories/ → ~/.nova/{agent_id}/
    - /project-memory/ → {workspace_root}/.nova/
    - /.nova/plans/ → {workspace_root}/.nova/plans/
    - Other absolute paths → resolved as-is
    - Relative paths → resolved relative to cwd
    """
    if not path_str:
        return None
    try:
        # /memories/ → agent directory
        if assistant_id and path_str.startswith("/memories/"):
            agent_dir = settings.get_agent_dir(assistant_id)
            suffix = path_str.removeprefix("/memories/").lstrip("/")
            return (agent_dir / suffix).resolve()

        # /project-memory/ → project .nova directory
        if path_str.startswith("/project-memory/"):
            project_root = settings.project_root or Path.cwd()
            suffix = path_str.removeprefix("/project-memory/").lstrip("/")
            return (project_root / ".nova" / suffix).resolve()

        # /.nova/plans/ → project plans directory
        if path_str.startswith("/.nova/plans/"):
            project_root = settings.project_root or Path.cwd()
            suffix = path_str.removeprefix("/.nova/plans/").lstrip("/")
            return (project_root / ".nova" / "plans" / suffix).resolve()

        path = Path(path_str)
        if path.is_absolute():
            return path
        return (Path.cwd() / path).resolve()
    except (OSError, ValueError):
        return None


def format_display_path(path_str: str | None) -> str:
    """Format a path for display."""
    if not path_str:
        return "(unknown)"
    try:
        path = Path(path_str)
        if path.is_absolute():
            return path.name or str(path)
        return str(path)
    except (OSError, ValueError):
        return str(path_str)


def build_approval_preview(
    tool_name: str,
    args: dict[str, Any],
    assistant_id: str | None,
) -> ApprovalPreview | None:
    """Collect summary info and diff for HITL approvals."""
    # Lazy import: deepagents is only needed to compute the replacement preview,
    # not at module import time (keeps startup fast).
    from deepagents.backends.utils import perform_string_replacement

    path_str = str(args.get("file_path") or args.get("path") or "")
    display_path = format_display_path(path_str)
    physical_path = resolve_physical_path(path_str, assistant_id)

    if tool_name == "write_file":
        content = str(args.get("content", ""))
        before = _safe_read(physical_path) if physical_path and physical_path.exists() else ""
        after = content
        diff = compute_unified_diff(before or "", after, display_path, max_lines=100)
        additions = 0
        if diff:
            additions = sum(
                1
                for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
        total_lines = _count_lines(after)
        details = [
            f"File: {path_str}",
            "Action: Create new file" + (" (overwrites existing content)" if before else ""),
            f"Lines to write: {additions or total_lines}",
        ]
        return ApprovalPreview(
            title=f"Write {display_path}",
            details=details,
            diff=diff,
            diff_title=f"Diff {display_path}",
        )

    if tool_name == "edit_file":
        if physical_path is None:
            return ApprovalPreview(
                title=f"Update {display_path}",
                details=[f"File: {path_str}", "Action: Replace text"],
                error="Unable to resolve file path.",
            )
        before = _safe_read(physical_path)
        if before is None:
            return ApprovalPreview(
                title=f"Update {display_path}",
                details=[f"File: {path_str}", "Action: Replace text"],
                error="Unable to read current file contents.",
            )
        old_string = str(args.get("old_string", ""))
        new_string = str(args.get("new_string", ""))
        replace_all = bool(args.get("replace_all", False))
        replacement = perform_string_replacement(before, old_string, new_string, replace_all)
        if isinstance(replacement, str):
            return ApprovalPreview(
                title=f"Update {display_path}",
                details=[f"File: {path_str}", "Action: Replace text"],
                error=replacement,
            )
        after, occurrences = replacement
        diff = compute_unified_diff(before, after, display_path, max_lines=None)
        additions = 0
        deletions = 0
        if diff:
            additions = sum(
                1
                for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            deletions = sum(
                1
                for line in diff.splitlines()
                if line.startswith("-") and not line.startswith("---")
            )
        details = [
            f"File: {path_str}",
            f"Action: Replace text ({'all occurrences' if replace_all else 'single occurrence'})",
            f"Occurrences matched: {occurrences}",
            f"Lines changed: +{additions} / -{deletions}",
        ]
        return ApprovalPreview(
            title=f"Update {display_path}",
            details=details,
            diff=diff,
            diff_title=f"Diff {display_path}",
        )

    return None


class FileOpTracker:
    """Collect file operation metrics during a CLI interaction."""

    def __init__(self, *, assistant_id: str | None, backend: BACKEND_TYPES | None = None) -> None:
        """Initialize the tracker."""
        self.assistant_id = assistant_id
        self.backend = backend
        self.active: dict[str | None, FileOperationRecord] = {}
        self.completed: list[FileOperationRecord] = []
        #: Completed operations performed *inside* another call, keyed by that
        #: call's id, waiting for its result to be rendered.
        self._nested: dict[str, list[FileOperationRecord]] = {}

    def start_operation(
        self,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str | None,
        *,
        parent_call_id: str | None = None,
    ) -> None:
        # Handle both lowercase (Nova-code) and capitalized (Serena MCP) tool names
        file_read_tools = {"read_file", "Read"}
        file_write_tools = WRITE_TOOLS
        all_file_tools = file_read_tools | file_write_tools

        if tool_name not in all_file_tools:
            return
        path_str = str(args.get("file_path") or args.get("path") or "")
        display_path = format_display_path(path_str)
        record = FileOperationRecord(
            tool_name=tool_name,
            display_path=display_path,
            physical_path=resolve_physical_path(path_str, self.assistant_id),
            tool_call_id=tool_call_id,
            args=args,
            parent_call_id=parent_call_id,
        )
        if tool_name in WRITE_TOOLS:
            if self.backend and path_str:
                try:
                    responses = self.backend.download_files([path_str])  # type: ignore
                    if (
                        responses
                        and responses[0].content is not None
                        and responses[0].error is None
                    ):
                        record.before_content = responses[0].content.decode("utf-8")
                    else:
                        record.before_content = ""
                except Exception:
                    record.before_content = ""
            elif record.physical_path:
                record.before_content = _safe_read(record.physical_path) or ""

        # Persist snapshot to ~/.nova/trash/ so content survives session end
        if record.before_content:
            try:
                from novacode_cli.recovery import get_recovery_manager

                mgr = get_recovery_manager()
                if mgr:
                    mgr.snapshot_from_content(path_str, record.before_content, reason=tool_name)
            except Exception:
                pass

        self.active[tool_call_id] = record

    def update_args(self, tool_call_id: str, args: dict[str, Any]) -> None:
        """Update arguments for an active operation and retry capturing before_content."""
        record = self.active.get(tool_call_id)
        if not record:
            return

        record.args.update(args)

        # If we haven't captured before_content yet, try again now that we might have the path
        if record.before_content is None and record.tool_name in {
            "write_file",
            "edit_file",
            "Write",
            "Edit",
        }:
            path_str = str(record.args.get("file_path") or record.args.get("path") or "")
            if path_str:
                record.display_path = format_display_path(path_str)
                record.physical_path = resolve_physical_path(path_str, self.assistant_id)
                if self.backend:
                    try:
                        responses = self.backend.download_files([path_str])  # type: ignore
                        if (
                            responses
                            and responses[0].content is not None
                            and responses[0].error is None
                        ):
                            record.before_content = responses[0].content.decode("utf-8")
                        else:
                            record.before_content = ""
                    except Exception:
                        record.before_content = ""
                elif record.physical_path:
                    record.before_content = _safe_read(record.physical_path) or ""

    def complete_with_message(self, tool_message: Any) -> FileOperationRecord | None:
        tool_call_id = getattr(tool_message, "tool_call_id", None)
        record = self.active.get(tool_call_id)
        if record is None:
            return None

        content = tool_message.content
        if isinstance(content, list):
            # Some tool messages may return list segments; join them for analysis.
            joined = []
            for item in content:
                if isinstance(item, str):
                    joined.append(item)
                else:
                    joined.append(str(item))
            content_text = "\n".join(joined)
        else:
            content_text = str(content) if content is not None else ""

        if getattr(
            tool_message, "status", "success"
        ) != "success" or content_text.lower().startswith("error"):
            record.status = "error"
            record.error = content_text
            self._finalize(record)
            return record

        record.status = "success"

        if record.tool_name in {"read_file", "Read"}:
            record.read_output = content_text
            # deepagents returns images as base64 content blocks and stamps the
            # source path into additional_kwargs — surface it for the preview
            # surfaces (TUI card, remote photo).
            media_kwargs = getattr(tool_message, "additional_kwargs", None) or {}
            media_path = media_kwargs.get("read_file_path")
            media_type = media_kwargs.get("read_file_media_type")
            if media_path and isinstance(media_type, str) and media_type.startswith("image/"):
                record.read_image = str(media_path)
            lines = _count_lines(content_text)
            record.metrics.lines_read = lines
            offset = record.args.get("offset")
            limit = record.args.get("limit")
            if isinstance(offset, int):
                record.metrics.start_line = offset + 1
                if lines:
                    record.metrics.end_line = offset + lines
            elif lines:
                record.metrics.start_line = 1
                record.metrics.end_line = lines
            if isinstance(limit, int) and lines > limit:
                record.metrics.end_line = (record.metrics.start_line or 1) + limit - 1
        else:
            # For write/edit operations, read back from backend (or local filesystem)
            self._populate_after_content(record)
            if record.after_content is None:
                record.status = "error"
                # Build a more informative error message
                error_parts = ["Could not read updated file content"]
                if self.backend:
                    error_parts.append("(backend download failed)")
                elif record.physical_path is None:
                    error_parts.append("(physical_path is None)")
                else:
                    error_parts.append(f"(filesystem read failed for {record.physical_path})")
                record.error = " ".join(error_parts) + "."
                logger.error(
                    f"File operation failed: tool={record.tool_name}, "
                    f"display_path={record.display_path}, physical_path={record.physical_path}, "
                    f"has_backend={self.backend is not None}"
                )
                self._finalize(record)
                return record
            record.metrics.lines_written = _count_lines(record.after_content)
            before_lines = _count_lines(record.before_content or "")
            diff = compute_unified_diff(
                record.before_content or "",
                record.after_content,
                record.display_path,
                max_lines=100,
            )
            record.diff = diff
            if diff:
                additions = sum(
                    1
                    for line in diff.splitlines()
                    if line.startswith("+") and not line.startswith("+++")
                )
                deletions = sum(
                    1
                    for line in diff.splitlines()
                    if line.startswith("-") and not line.startswith("---")
                )
                record.metrics.lines_added = additions
                record.metrics.lines_removed = deletions
            elif (
                record.tool_name in {"write_file", "Write"} and (record.before_content or "") == ""
            ):
                record.metrics.lines_added = record.metrics.lines_written
            record.metrics.bytes_written = len(record.after_content.encode("utf-8"))
            if record.diff is None and (record.before_content or "") != record.after_content:
                record.diff = compute_unified_diff(
                    record.before_content or "",
                    record.after_content,
                    record.display_path,
                    max_lines=100,
                )
            if record.diff is None and before_lines != record.metrics.lines_written:
                record.metrics.lines_added = max(record.metrics.lines_written - before_lines, 0)

        self._finalize(record)
        return record

    def mark_hitl_approved(self, tool_name: str, args: dict[str, Any]) -> None:
        """Mark operations matching tool_name and file_path as HIL-approved."""
        file_path = args.get("file_path") or args.get("path")
        if not file_path:
            return

        # Normalize tool name for comparison (handle both lowercase and capitalized)
        normalized_tool = tool_name.lower() if tool_name in {"Write", "Edit", "Read"} else tool_name

        # Mark all active records that match
        for record in self.active.values():
            record_tool = (
                record.tool_name.lower()
                if record.tool_name in {"Write", "Edit", "Read"}
                else record.tool_name
            )
            if record_tool == normalized_tool:
                record_path = record.args.get("file_path") or record.args.get("path")
                if record_path == file_path:
                    record.hitl_approved = True

    def _populate_after_content(self, record: FileOperationRecord) -> None:
        # Use backend if available (works for any BackendProtocol implementation)
        if self.backend:
            try:
                file_path = record.args.get("file_path") or record.args.get("path")
                if file_path:
                    responses = self.backend.download_files([file_path])  # type: ignore
                    if (
                        responses
                        and responses[0].content is not None
                        and responses[0].error is None
                    ):
                        record.after_content = responses[0].content.decode("utf-8")
                        return
                    else:
                        # Log why backend failed
                        if responses:
                            error = responses[0].error if responses else "no response"
                            content_status = (
                                "None"
                                if responses[0].content is None
                                else f"{len(responses[0].content)} bytes"
                            )
                            logger.warning(
                                f"Backend download_files failed: path={file_path}, error={error}, content={content_status}"
                            )
                        else:
                            logger.warning(
                                f"Backend download_files returned empty responses for: {file_path}"
                            )
            except Exception as e:
                logger.warning(f"Backend download_files exception for {file_path}: {e}")
                pass  # Fall through to filesystem fallback

        # Fallback: direct filesystem read when no backend or backend failed
        if record.physical_path is None:
            logger.warning(
                f"Cannot read back file - physical_path is None: display_path={record.display_path}, tool_name={record.tool_name}"
            )
            record.after_content = None
            return
        record.after_content = _safe_read(record.physical_path)
        if record.after_content is None:
            logger.warning(f"Failed to read back file from filesystem: {record.physical_path}")

    def take_nested(self, parent_call_id: str | None) -> list[FileOperationRecord]:
        """Operations performed inside ``parent_call_id``, removed as they leave.

        Taking them means a report emitted twice (a replayed or re-read message,
        a resumed turn) cannot render the same diff twice.
        """
        if not parent_call_id:
            return []
        return self._nested.pop(parent_call_id, [])

    def abandon(self, tool_call_id: str | None) -> None:
        """Drop a started operation whose outcome is unknown (a timeout, a cancel).

        No record is produced: the write may or may not have landed, and reading
        the file back would describe a guess as a change.
        """
        self.active.pop(tool_call_id, None)

    def _finalize(self, record: FileOperationRecord) -> None:
        self.completed.append(record)
        self.active.pop(record.tool_call_id, None)
        if record.parent_call_id and record.tool_call_id:
            self._nested.setdefault(record.parent_call_id, []).append(record)
        # Bound the completed list: it is write-only (no display consumer reads
        # it), but each record can hold full file contents (before_content,
        # after_content, read_output). Keeping every record for a long session
        # compounds memory. Retain only the most recent records and drop the
        # heavy content fields from the ones we evict.
        if len(self.completed) > _MAX_COMPLETED_RECORDS:
            evicted = self.completed[:-_MAX_COMPLETED_RECORDS]
            for old in evicted:
                old.before_content = None
                old.after_content = None
                old.read_output = None
                old.diff = None
            del self.completed[:-_MAX_COMPLETED_RECORDS]


# ============================================================================
# Session-Level FileOpTracker (shared between main agent and subagents)
# ============================================================================

_session_file_op_trackers: dict[str | None, FileOpTracker] = {}
#: The tracker the running turn resolved last. Code Mode's nested calls are made
#: by middleware that does not know the owning agent's id, and asking for a
#: tracker under a *different* key would resolve paths against a different
#: backend (see the keying note below). This is that same object, not a copy.
_active_tracker: FileOpTracker | None = None


def get_session_file_op_tracker(
    assistant_id: str | None = None,
    backend: BACKEND_TYPES | None = None,
) -> FileOpTracker:
    """Get or create the session-level FileOpTracker.

    This ensures the main agent and all subagents share the same tracker,
    so file operations from subagents are properly tracked and displayed.

    Args:
        assistant_id: Optional assistant ID for the tracker.
        backend: Optional backend for file operations.

    Returns:
        The session-level FileOpTracker instance.
    """
    # Keyed by assistant_id, NOT a single process global: the main-thread TUI
    # agent ("nova-agent") and the cowork server agent ("nova-server") run
    # concurrently in one process with DIFFERENT backends/workspace roots. A
    # single shared tracker meant whichever agent ran first "owned" it, so the
    # other resolved file ops against the wrong backend/root → "Agent run failed".
    # Keying isolates them; the main agent and its subagents still share (same id).
    tracker = _session_file_op_trackers.get(assistant_id)
    if tracker is None:
        tracker = FileOpTracker(assistant_id=assistant_id, backend=backend)
        _session_file_op_trackers[assistant_id] = tracker
    global _active_tracker  # noqa: PLW0603 — one pointer to the tracker just resolved
    _active_tracker = tracker
    return tracker


def current_file_op_tracker() -> FileOpTracker | None:
    """The tracker this turn is already using, or None before one exists.

    Callers that have no assistant id (Code Mode's nested dispatch) use this
    instead of :func:`get_session_file_op_tracker`, which would create a second
    tracker under a ``None`` key and resolve paths against the wrong backend.
    """
    return _active_tracker


def reset_session_file_op_tracker(assistant_id: str | None = None) -> None:
    """Reset session-level FileOpTracker(s).

    With no argument, clears every tracker (start of a fresh process/session);
    with an *assistant_id*, drops only that agent's tracker.
    """
    if assistant_id is None:
        _session_file_op_trackers.clear()
        global _active_tracker  # noqa: PLW0603 — dropped with the trackers it points at
        _active_tracker = None
    else:
        _session_file_op_trackers.pop(assistant_id, None)
