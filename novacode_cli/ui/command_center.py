"""Saved and live conversations, shaped once for both frontends.

The Textual modal and the socket port list the same thing: every saved session
overlaid with the tabs that are live right now. This module owns that shaping —
merge, order, status labels, ages — and nothing else: no widget, no frame, no
I/O. Keeping it here is what stops the two frontends from disagreeing about
which conversation is working and which one needs a person.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from novacode_cli.session.session_persistence import SessionMeta

STATUSES: tuple[str, ...] = ("All", "Needs you", "Working", "Ready", "Inactive")

# How much of a message the details pane shows before it clips.
DETAIL_CHARS = 1600

# A `sessions` row reports the process, not the conversation. These are the four
# labels a person reads, so one vocabulary serves the tab bar and both lists.
_REGISTRY_STATUS = {
    "running": "Working",
    "starting": "Working",
    "needs-approval": "Needs you",
    "crashed": "Inactive",
    "exited": "Inactive",
}


@dataclass(frozen=True)
class CommandSession:
    """Detached display facts; selecting one grants no execution permission."""

    key: str
    session_id: str = ""
    pane_id: str = ""
    current: bool = False
    title: str = ""
    project: str = ""
    status: str = "Inactive"
    updated_at: str = ""
    model: str = ""
    reasoning: str = ""
    branch: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    last_message: str = ""
    prompt: str = ""
    blocked_reason: str = ""
    external: bool = False


def message_text(content: object) -> str:
    """The text of a message's content, ignoring tool and reasoning blocks."""
    if isinstance(content, str):
        return content[:DETAIL_CHARS]
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )[:DETAIL_CHARS]
    return ""


def message_preview(messages: Sequence[object]) -> tuple[str, str]:
    """Use only human/assistant text, excluding tools and hidden reasoning blocks."""
    prompt = reply = ""
    for message in reversed(messages):
        kind = getattr(message, "type", "")
        if kind == "human" and not prompt:
            prompt = message_text(getattr(message, "content", ""))
        elif kind == "ai" and not reply:
            reply = message_text(getattr(message, "content", ""))
        if prompt and reply:
            break
    return prompt, reply


def safe_identity(identity: str) -> bool:
    """Whether a session id can name a directory entry without escaping it."""
    return (
        isinstance(identity, str)
        and bool(identity)
        and identity not in {".", ".."}
        and not any(character in identity for character in ("/", "\\", ":", "\0"))
    )


def merge_sessions(
    saved: Iterable[SessionMeta], live: Iterable[CommandSession]
) -> list[CommandSession]:
    """Overlay live facts; an old saved 'active' flag is never proof of running work."""
    rows = {
        f"session:{meta.session_id}": CommandSession(
            key=f"session:{meta.session_id}",
            session_id=meta.session_id,
            title=meta.current_task or "Saved conversation",
            project=meta.project_root or "",
            status="Needs you" if meta.task_status == "blocked" else "Inactive",
            updated_at=meta.last_active,
            model=meta.model_name or "",
            blocked_reason=meta.blocked_reason or "",
        )
        for meta in saved
        if not meta.cleared and safe_identity(meta.session_id)
    }
    for live_row in live:
        row = live_row
        previous = rows.get(row.key)
        if previous is not None:
            row = replace(
                row,
                title=row.title or previous.title,
                project=row.project or previous.project,
                model=row.model or previous.model,
                updated_at=row.updated_at or previous.updated_at,
            )
        rows[row.key] = replace(row, title=row.title or "Current conversation")
    ordered = sorted(rows.values(), key=lambda row: row.updated_at, reverse=True)
    return sorted(ordered, key=lambda row: (not row.current, not bool(row.pane_id)))


def registry_sessions(
    rows: Iterable[dict[str, object]], active: str | None = None
) -> list[CommandSession]:
    """The live tabs of one server, from its `sessions` rows.

    ``pane_id`` carries the session id here: for a server a tab *is* a session,
    and the field only means "this one is live", which is how both lists mark it.

    Args:
        rows: `{"id", "name", "cwd", "status", "model"}` dicts.
        active: The id this connection is attached to.

    Returns:
        One row per usable session, in the registry's order.
    """
    found = []
    for row in rows:
        session_id = str(row.get("id") or "")
        if not session_id:
            continue
        found.append(
            CommandSession(
                key=f"session:{session_id}",
                session_id=session_id,
                pane_id=session_id,
                current=session_id == active,
                title=str(row.get("name") or session_id[:8]),
                project=str(row.get("cwd") or ""),
                status=_REGISTRY_STATUS.get(str(row.get("status") or ""), "Ready"),
                model=str(row.get("model") or ""),
            )
        )
    return found


def session_age(timestamp: str) -> str:
    """Render malformed/legacy dates honestly, without failing a whole list."""
    if not timestamp:
        return "Live"
    try:
        instant = datetime.fromisoformat(timestamp)
        instant = instant.replace(tzinfo=UTC) if instant.tzinfo is None else instant
        seconds = max(0, int((datetime.now(UTC) - instant).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return "Unknown"
    for divisor, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= divisor:
            return f"{seconds // divisor}{unit} ago"
    return f"{seconds}s ago"


def filter_sessions(
    rows: Sequence[CommandSession], status: str = STATUSES[0], *, terms: Sequence[str] = ()
) -> list[CommandSession]:
    """Rows matching a status filter and every search term, in the given order."""
    wanted = [term.casefold() for term in terms if term]
    return [
        row
        for row in rows
        if status in ("All", row.status)
        and all(
            term in f"{row.title} {row.project} {row.model} {row.session_id}".casefold()
            for term in wanted
        )
    ]


def status_counts(rows: Sequence[CommandSession]) -> list[tuple[str, int]]:
    """The filter bar's labels and counts, in :data:`STATUSES` order."""
    return [
        (status, len(rows) if status == "All" else sum(row.status == status for row in rows))
        for status in STATUSES
    ]


def describe(row: CommandSession) -> str:
    """One line of detail: status, project, age and model, without empty parts."""
    age = session_age(row.updated_at)
    parts = [
        row.status,
        row.project or "No project",
        "live" if row.pane_id else age,
        row.model,
    ]
    if row.blocked_reason:
        parts.insert(1, row.blocked_reason)
    return " · ".join(str(part) for part in parts if part)
