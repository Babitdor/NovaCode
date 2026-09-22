"""File Tracker Middleware for enforcing read-before-edit and tracking file operations.

This middleware provides:
1. Hard enforcement of read-before-edit (rejects edits for unread files)
2. File content caching for edit verification
3. Session-level file operation tracking
4. Smart tool result truncation to prevent context overflow
"""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain.tools import BaseTool
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class FileReadRecord:
    """Record of a file read operation."""

    path: str
    """Absolute path to the file."""

    content_hash: str
    """SHA-256 hash of the file content when read."""

    line_count: int
    """Number of lines in the file."""

    char_count: int
    """Number of characters in the file."""

    read_at: str
    """ISO timestamp when the file was read."""

    offset: int = 0
    """Starting line offset (0-based)."""

    limit: int | None = None
    """Number of lines read (None = full file)."""

    content_preview: str = ""
    """First 500 chars of content for context."""


@dataclass
class FileWriteRecord:
    """Record of a file write operation."""

    path: str
    """Absolute path to the file."""

    operation: str
    """Type of operation: 'write', 'edit', 'create'."""

    content_hash: str
    """SHA-256 hash of the new content."""

    written_at: str
    """ISO timestamp when the file was written."""

    old_content_hash: str | None = None
    """Hash of content before edit (for edit operations)."""

    lines_changed: int = 0
    """Number of lines affected."""


@dataclass
class SessionFileTracker:
    """Tracks all file operations in the current session."""

    files_read: dict[str, FileReadRecord] = field(default_factory=dict)
    """Map of file path -> most recent read record."""

    files_written: dict[str, list[FileWriteRecord]] = field(default_factory=dict)
    """Map of file path -> list of write records (history)."""

    read_order: list[str] = field(default_factory=list)
    """Order in which files were first read."""

    write_order: list[str] = field(default_factory=list)
    """Order in which files were first written."""

    total_reads: int = 0
    """Total number of read operations."""

    total_writes: int = 0
    """Total number of write operations."""

    rejected_edits: int = 0
    """Number of edit operations rejected for unread files."""

    def has_read_file(self, path: str) -> bool:
        """Check if a file has been read in this session."""
        return path in self.files_read

    def get_read_record(self, path: str) -> FileReadRecord | None:
        """Get the read record for a file."""
        return self.files_read.get(path)

    def record_read(
        self,
        path: str,
        content: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> FileReadRecord:
        """Record a file read operation."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        lines = content.split("\n")

        record = FileReadRecord(
            path=path,
            content_hash=content_hash,
            line_count=len(lines),
            char_count=len(content),
            read_at=datetime.now(UTC).isoformat(),
            offset=offset,
            limit=limit,
            content_preview=content[:500] if content else "",
        )

        # Track first read order
        if path not in self.files_read:
            self.read_order.append(path)

        self.files_read[path] = record
        self.total_reads += 1

        return record

    def record_write(
        self,
        path: str,
        content: str,
        operation: str = "write",
        old_content: str | None = None,
        lines_changed: int = 0,
    ) -> FileWriteRecord:
        """Record a file write operation."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        old_hash = hashlib.sha256(old_content.encode()).hexdigest()[:16] if old_content else None

        record = FileWriteRecord(
            path=path,
            operation=operation,
            content_hash=content_hash,
            written_at=datetime.now(UTC).isoformat(),
            old_content_hash=old_hash,
            lines_changed=lines_changed,
        )

        # Track first write order
        if path not in self.files_written:
            self.write_order.append(path)
            self.files_written[path] = []

        self.files_written[path].append(record)
        self.total_writes += 1

        return record

    def record_rejected_edit(self) -> None:
        """Record a rejected edit attempt."""
        self.rejected_edits += 1

    def get_session_summary(self) -> str:
        """Get a summary of file operations in this session."""
        lines = ["## Session File Operations Summary\n"]

        if self.files_read:
            lines.append(f"### Files Read ({len(self.files_read)})")
            for path in self.read_order[-10:]:  # Last 10 reads
                record = self.files_read[path]
                lines.append(f"- `{path}` ({record.line_count} lines)")
            if len(self.read_order) > 10:
                lines.append(f"  ... and {len(self.read_order) - 10} more")
            lines.append("")

        if self.files_written:
            lines.append(f"### Files Modified ({len(self.files_written)})")
            for path in self.write_order[-10:]:  # Last 10 writes
                records = self.files_written[path]
                ops = [r.operation for r in records]
                lines.append(f"- `{path}` ({', '.join(ops)})")
            if len(self.write_order) > 10:
                lines.append(f"  ... and {len(self.write_order) - 10} more")
            lines.append("")

        lines.append(f"**Stats**: {self.total_reads} reads, {self.total_writes} writes")
        if self.rejected_edits > 0:
            lines.append(f"**Rejected edits** (unread files): {self.rejected_edits}")

        return "\n".join(lines)


# ============================================================================
# Module-level singleton
# ============================================================================

_session_tracker: SessionFileTracker | None = None


def get_session_tracker() -> SessionFileTracker:
    """Get or create the session file tracker."""
    global _session_tracker
    if _session_tracker is None:
        _session_tracker = SessionFileTracker()
    return _session_tracker


def reset_session_tracker() -> None:
    """Reset the session tracker (for new sessions)."""
    global _session_tracker
    _session_tracker = None


# ============================================================================
# Tool Result Truncation
# ============================================================================

# deepagents' FilesystemMiddleware offloads a tool result to the backend once it
# exceeds ``tool_token_limit_before_evict`` (default 20k tokens) and replaces it
# with a file path plus a head/tail preview — a RECOVERABLE eviction, unlike
# truncation, which destroys the tail outright.
#
# Crucially, that eviction does NOT apply to every tool. deepagents ships
#
#     TOOLS_EXCLUDED_FROM_EVICTION = ("ls", "glob", "grep", "read_file",
#                                     "edit_file", "write_file", "delete")
#
# with a deliberate rationale: read_file's failure mode is single very long lines
# (e.g. a jsonl with huge payloads), and truncating it makes the agent re-read the
# truncated file and get nowhere. So for `read_file` there is NO offload safety
# net, and Nova's own char cap is the ONLY bound on what enters context.
#
# Caps are therefore set PER TOOL, not uniformly:
#   * Never-evicted tools (`read_file`) — keep a TIGHT cap, because nothing else
#     will stop the result. Raising it lets a huge tool result into context
#     permanently, which is strictly worse than truncating it.
#   * Evictable tools (`execute`, `shell`, `fetch_url`) — cap ABOVE the offload
#     threshold so the recoverable path fires instead of losing the content here.
#   * Self-truncating tools (`ls`, `glob`, `grep`) — tight caps; their own
#     truncation already fired, and a pointer to noise is not worth the round trip.
# Mirrors deepagents' TOOLS_EXCLUDED_FROM_EVICTION (deepagents/middleware/filesystem.py).
OFFLOAD_THRESHOLD_CHARS = 80_000

# Tools deepagents never offloads — for these Nova's cap is the only bound, so
# it must stay tight. Keep in sync with deepagents' TOOLS_EXCLUDED_FROM_EVICTION.
TOOLS_EXCLUDED_FROM_EVICTION = frozenset(
    {"ls", "glob", "grep", "read_file", "edit_file", "write_file", "delete"}
)

# Maximum characters for different tool result types
RESULT_LIMITS = {
    # Above OFFLOAD_THRESHOLD_CHARS: these ARE evicted by deepagents, so let the
    # result reach the threshold and be offloaded with a recovery pointer.
    "shell": 160_000,  # ~40k tokens — reaches eviction
    "execute": 160_000,
    "fetch_url": 160_000,
    "default": 160_000,
    # read_file is in TOOLS_EXCLUDED_FROM_EVICTION: no offload will ever happen,
    # so this cap is the only thing bounding it. Kept tight on purpose.
    "read_file": 50_000,  # ~12.5k tokens — the sole guard
    # Self-truncating / cheap to re-derive; no pointer is worth them.
    "grep": 20000,  # ~5k tokens
    "glob": 10000,  # ~2.5k tokens
    "ls": 8000,  # ~2k tokens
    "web_search": 15000,  # ~3.75k tokens
}


def truncate_tool_result(
    tool_name: str,
    result: str,
    custom_limit: int | None = None,
) -> tuple[str, bool]:
    """Truncate a tool result if it exceeds the limit.

    Uses an incremental scan so large results (10MB+) are never fully materialised
    before truncation — the scan stops as soon as the char limit is reached.

    Args:
        tool_name: Name of the tool that produced the result.
        result: The tool result string.
        custom_limit: Optional custom character limit.

    Returns:
        Tuple of (possibly truncated result, was_truncated).
    """
    limit = custom_limit or RESULT_LIMITS.get(tool_name, RESULT_LIMITS["default"])

    if len(result) <= limit:
        return result, False

    # Leave room for the truncation message (~500 chars)
    target = limit - 500

    # Walk forward char-by-char until we hit the target, tracking the last
    # newline position so we can snap to a clean line boundary.
    last_newline = -1
    pos = 0
    for pos, ch in enumerate(result):
        if pos >= target:
            break
        if ch == "\n":
            last_newline = pos

    # Snap to line boundary if one exists within 80% of the target
    truncate_at = last_newline if last_newline > target * 0.8 else target

    truncated = result[:truncate_at]
    chars_removed = len(result) - truncate_at

    # Count removed lines without scanning the full tail string
    lines_removed = result.count("\n", truncate_at)
    kept_lines = truncated.count("\n")

    truncation_msg = f"""

... [TRUNCATED: {chars_removed:,} characters, ~{lines_removed} lines removed]

**To see more content:**
- Use pagination: `read_file(path, offset={kept_lines}, limit=200)`
- Use grep to search for specific patterns
- Use glob to find specific files"""

    return truncated + truncation_msg, True

def _has_offload_pointer(content: str) -> bool:
    """True when ``content`` is an offload notice rather than a raw tool result.

    deepagents' eviction message opens with a fixed sentence and names the file
    the payload was written to. Truncating it would strip the path and destroy
    the only route back to the content, so truncation must stand down.
    """
    head = content[:400]
    return (
        "was saved in the filesystem at this path" in head
        or "<large_tool_result" in head
    ) and "/large_tool_results/" in content[:1000]


# ============================================================================
# State Schema
# ============================================================================


class FileTrackerState(AgentState):
    """State schema for file tracker middleware."""

    _file_tracker: NotRequired[SessionFileTracker]
    """The session file tracker instance."""


# ============================================================================
# Middleware Implementation
# ============================================================================

# File tracker system prompt loaded from: NovaCode_cli/prompts/file_tracker.jinja


class FileTrackerMiddleware(AgentMiddleware):
    """Middleware that enforces read-before-edit and tracks file operations.

    This middleware:
    1. Tracks all file read operations
    2. Enforces that files must be read before editing
    3. Truncates large tool results to prevent context overflow
    4. Provides session-level file operation summary
    5. Injects file operation rules into the system prompt

    Args:
        enforce_read_before_edit: Whether to reject edits on unread files (default: True).
        truncate_results: Whether to truncate large tool results (default: True).
        include_system_prompt: Whether to inject file operation rules (default: True).
        tracker: Optional custom SessionFileTracker (uses singleton if None).
    """

    state_schema = FileTrackerState

    def __init__(
        self,
        enforce_read_before_edit: bool = True,
        truncate_results: bool = True,
        include_system_prompt: bool = True,
        tracker: SessionFileTracker | None = None,
    ) -> None:
        super().__init__()
        self.enforce_read_before_edit = enforce_read_before_edit
        self.truncate_results = truncate_results
        self.include_system_prompt = include_system_prompt
        self._tracker = tracker
        self.tools: list[BaseTool] = []  # type: ignore # No additional tools
        # Cache for rendered prompt to avoid re-rendering on every request
        self._prompt_cache: str | None = None
        self._prompt_cache_time: float = 0
        self._prompt_cache_ttl: float = 30.0  # 30 seconds TTL

    @property
    def tracker(self) -> SessionFileTracker:
        """Get the session tracker."""
        if self._tracker is not None:
            return self._tracker
        return get_session_tracker()

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject file operation rules into the system prompt and sanitize messages."""
        # Sanitize any file-type content blocks in message history (e.g., from
        # restored sessions) so they don't crash backends like Ollama
        from novacode_cli.utils.pdf_extraction import sanitize_messages_file_blocks

        sanitize_messages_file_blocks(request.messages)

        if self.include_system_prompt:
            import time

            current_time = time.time()

            # Use cached prompt if still valid (sliding window: refreshes on access)
            if (
                self._prompt_cache is not None
                and current_time - self._prompt_cache_time < self._prompt_cache_ttl
            ):
                # Sliding window: reset timer on access to keep cache alive during active use
                self._prompt_cache_time = current_time
                file_tracker_prompt = self._prompt_cache
            else:
                from novacode_cli.prompts import render_template

                file_tracker_prompt = render_template("file_tracker.jinja")
                self._prompt_cache = file_tracker_prompt
                self._prompt_cache_time = current_time

            system_prompt = (
                request.system_prompt + "\n\n" + file_tracker_prompt
                if request.system_prompt
                else file_tracker_prompt
            )
            return handler(request.override(system_prompt=system_prompt))
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """(async) Inject file operation rules into the system prompt and sanitize messages."""
        # Sanitize any file-type content blocks in message history (e.g., from
        # restored sessions) so they don't crash backends like Ollama
        from novacode_cli.utils.pdf_extraction import sanitize_messages_file_blocks

        sanitize_messages_file_blocks(request.messages)

        if self.include_system_prompt:
            import time

            current_time = time.time()

            # Use cached prompt if still valid (sliding window: refreshes on access)
            if (
                self._prompt_cache is not None
                and current_time - self._prompt_cache_time < self._prompt_cache_ttl
            ):
                # Sliding window: reset timer on access to keep cache alive during active use
                self._prompt_cache_time = current_time
                file_tracker_prompt = self._prompt_cache
            else:
                from novacode_cli.prompts import render_template

                file_tracker_prompt = render_template("file_tracker.jinja")
                self._prompt_cache = file_tracker_prompt
                self._prompt_cache_time = current_time

            system_prompt = (
                request.system_prompt + "\n\n" + file_tracker_prompt
                if request.system_prompt
                else file_tracker_prompt
            )
            return await handler(request.override(system_prompt=system_prompt))
        return await handler(request)

    def _check_edit_allowed(self, request: ToolCallRequest) -> ToolMessage | None:
        """Check if an edit operation is allowed (file has been read).

        Returns ToolMessage with rejection if not allowed, None if allowed.
        """
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})

        # Only check edit_file operations
        if tool_name != "edit_file" or not self.enforce_read_before_edit:
            return None

        file_path = args.get("file_path") or args.get("path", "")
        # A file the agent has written or previously edited this session is known
        # content — treat it like a read so write_file→edit_file (a very common
        # pattern) isn't falsely rejected. Only genuinely-unseen files are gated.
        if (
            file_path
            and not self.tracker.has_read_file(file_path)
            and file_path not in self.tracker.files_written
        ):
            self.tracker.record_rejected_edit()
            return ToolMessage(
                content=(
                    f"**EDIT REJECTED**: File '{file_path}' has not been read in this session.\n\n"
                    f"You must read a file before editing it. This prevents errors from:\n"
                    f"- Editing stale content\n"
                    f"- Using incorrect old_string values\n"
                    f"- Modifying the wrong file\n\n"
                    f'**To fix**: First run `read_file("{file_path}")`, then retry your edit.'
                ),
                tool_call_id=tool_call.get("id", ""),
                # Mark as an error so the UI shows it as a failed edit rather than
                # a silent ✓ with no diff (the file is unchanged on rejection).
                status="error",
            )

        return None

    def _track_and_truncate(
        self, request: ToolCallRequest, result: ToolMessage | Command
    ) -> ToolMessage | Command:
        """Track file operations and optionally truncate results."""
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})

        # Get content from result
        if isinstance(result, ToolMessage):
            content = result.content
        elif isinstance(result, Command):
            # For Command results, we don't modify
            return result
        else:
            return result

        # Convert PDF and other "file" type content blocks to text.
        # Ollama (and some other backends) don't support type="file" content
        # blocks, so we extract text from PDFs before they enter the message
        # history — preventing a fatal "Blocks of type file not supported" crash.
        if isinstance(content, list) and tool_name == "read_file":
            from novacode_cli.utils.pdf_extraction import convert_file_content_block_to_text

            converted = convert_file_content_block_to_text(content)
            if converted is not content:  # something was converted
                # Rebuild ToolMessage with converted content and preserve metadata
                result = ToolMessage(
                    content=converted,
                    tool_call_id=result.tool_call_id,
                    name=result.name if hasattr(result, "name") else None,
                )
                content = converted

        # Track read_file operations (text string or extracted PDF text)
        if tool_name == "read_file" and not (
            isinstance(content, str) and content.startswith("Error")
        ):
            file_path = args.get("file_path") or args.get("path", "")
            offset = args.get("offset", 0)
            limit = args.get("limit")
            if file_path:
                # For string content, track directly
                # For list content (e.g., extracted PDF), use a summary
                if isinstance(content, str):
                    self.tracker.record_read(file_path, content, offset, limit)
                elif isinstance(content, list):
                    # Compose a text summary from text blocks for tracking
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    tracking_text = "\n".join(text_parts) or "[binary file read]"
                    self.tracker.record_read(file_path, tracking_text, offset, limit)

        # Track write_file operations
        if tool_name == "write_file" and isinstance(content, str) and "success" in content.lower():
            file_path = args.get("file_path") or args.get("path", "")
            new_content = args.get("content", "")
            if file_path:
                self.tracker.record_write(file_path, new_content, operation="write")

        # Track edit_file operations
        if tool_name == "edit_file" and isinstance(content, str) and "success" in content.lower():
            file_path = args.get("file_path") or args.get("path", "")
            if file_path:
                self.tracker.record_write(
                    file_path,
                    args.get("new_string", ""),
                    operation="edit",
                    old_content=args.get("old_string"),
                )

        # Truncate large results — but NEVER when the result already carries a
        # recovery pointer. deepagents' FilesystemMiddleware replaces an
        # oversized result with a path + head/tail preview; truncating that
        # string would cut the path off and turn a *recoverable* offload into a
        # dead end. The pointer is strictly more useful than a truncation.
        if self.truncate_results and isinstance(content, str) and not _has_offload_pointer(content):
            truncated_content, was_truncated = truncate_tool_result(tool_name, content)
            if was_truncated and isinstance(result, ToolMessage):
                return ToolMessage(
                    content=truncated_content,
                    tool_call_id=result.tool_call_id,
                    name=result.name if hasattr(result, "name") else None,
                )

        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept tool calls to enforce read-before-edit and track operations."""
        # Check if edit is allowed
        rejection = self._check_edit_allowed(request)
        if rejection is not None:
            return rejection

        # Execute the tool
        result = handler(request)

        # Track operations and truncate if needed
        return self._track_and_truncate(request, result)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """(async) Intercept tool calls to enforce read-before-edit and track operations."""
        # Check if edit is allowed
        rejection = self._check_edit_allowed(request)
        if rejection is not None:
            return rejection

        # Execute the tool
        result = await handler(request)

        # Track operations and truncate if needed
        return self._track_and_truncate(request, result)


# ============================================================================
# Utility Functions
# ============================================================================


def get_files_summary() -> str:
    """Get a summary of file operations for the current session."""
    return get_session_tracker().get_session_summary()


def get_recently_read_files(limit: int = 10) -> list[str]:
    """Get the most recently read file paths."""
    tracker = get_session_tracker()
    return tracker.read_order[-limit:]


def get_modified_files() -> list[str]:
    """Get all files modified in this session."""
    tracker = get_session_tracker()
    return list(tracker.files_written.keys())


def was_file_read(path: str) -> bool:
    """Check if a file was read in this session."""
    return get_session_tracker().has_read_file(path)


__all__ = [
    "RESULT_LIMITS",
    "FileReadRecord",
    "FileTrackerMiddleware",
    "FileWriteRecord",
    "SessionFileTracker",
    "get_files_summary",
    "get_modified_files",
    "get_recently_read_files",
    "get_session_tracker",
    "reset_session_tracker",
    "truncate_tool_result",
    "was_file_read",
]
