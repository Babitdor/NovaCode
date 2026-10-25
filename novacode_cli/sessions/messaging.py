"""Bounded, parent-owned communication between independent project sessions."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_TEXT_BYTES = 16 * 1024
MAX_REPLY_BYTES = 64 * 1024
MAX_HOPS = 8
FINAL = frozenset({"completed", "failed", "cancelled"})


class MessageError(ValueError):
    """An address or message cannot be safely delivered."""


@dataclass(frozen=True)
class SessionInfo:
    """Public address and status metadata, with no permission or history state."""

    sid: str
    title: str
    project: str
    status: str = "idle"


@dataclass
class SessionMessage:
    """One explicit request and its correlated result."""

    id: str
    sender: str
    recipient: str
    text: str
    status: str = "queued"
    response: str = ""
    version: int = 0
    depth: int = 0


def resolve_target(name: str, sessions: dict[str, str]) -> str:
    """Resolve stable IDs before unique titles, rejecting ambiguous addresses."""
    if not isinstance(name, str) or not name.strip():
        msg = "A tab address is required."
        raise MessageError(msg)
    if name in sessions:
        return name
    if name.lower() in {"main", "root"} and "root" in sessions:
        return "root"
    matches = [sid for sid, title in sessions.items() if title.casefold() == name.casefold()]
    if len(matches) > 1:
        msg = f"Ambiguous tab '{name}'; use @tab:<session-id>."
        raise MessageError(msg)
    if not matches:
        msg = f"Unknown tab '{name}'."
        raise MessageError(msg)
    return matches[0]


def resolve_mention(
    text: str,
    sessions: dict[str, str],
    reserved: Iterable[str] = (),
) -> tuple[str, str] | None:
    """Only leading mentions address a tab; explicit addresses never fall through."""
    match = re.match(r'^@(tab:)?("(?:[^"\\]|\\.)*"|[^\s]+)(?:\s+([\s\S]+))?$', text.strip())
    if text.strip().startswith("@tab:") and (match is None or not match.group(1)):
        msg = "A tab address needs a session ID and message."
        raise MessageError(msg)
    if match is None:
        return None
    explicit, name, body = match.groups()
    if name.startswith('"'):
        if not explicit:
            return None  # Quoted file mentions keep their existing attachment path.
        try:
            name = json.loads(name)
        except ValueError as exc:
            msg = "Use a quoted session name or a session ID after @tab:."
            raise MessageError(msg) from exc
    if not explicit:
        if name.casefold() in {item.casefold() for item in reserved}:
            return None
        if name not in sessions and name.casefold() not in {
            title.casefold() for title in sessions.values()
        }:
            return None
    sid = resolve_target(name, sessions)
    if not body or not body.strip():
        msg = "A tab address needs a message."
        raise MessageError(msg)
    return sid, body.strip()


class SessionBroker:
    """Session addressing and bounded request/reply bookkeeping."""

    def __init__(
        self, *, max_messages: int = 256, max_pending: int = 32, max_bytes: int = 8 * 1024 * 1024
    ) -> None:
        """Set hard bounds for retained requests, per-recipient work and bytes."""
        self.sessions: dict[str, SessionInfo] = {}
        self.messages: dict[str, SessionMessage] = {}
        self.max_messages, self.max_pending, self.max_bytes = max_messages, max_pending, max_bytes
        self._counter = 0
        self._seen: dict[tuple[str, str], int] = {}

    @property
    def retained_bytes(self) -> int:
        """Count retained UTF-8 payload bytes."""
        return sum(len((m.text + m.response).encode("utf-8")) for m in self.messages.values())

    def update_sessions(self, sessions: list[SessionInfo]) -> None:
        """Refresh current addresses and fail requests involving a closed session."""
        self.sessions = {s.sid: s for s in sessions if s.status not in {"exited", "crashed"}}
        for message in self.messages.values():
            if message.status not in FINAL and (
                message.sender not in self.sessions or message.recipient not in self.sessions
            ):
                message.status = "failed"
                message.version += 1
        self._seen = {key: value for key, value in self._seen.items() if key[0] in self.sessions}

    def resolve_mention(self, text: str, reserved: Iterable[str] = ()) -> tuple[str, str] | None:
        """Resolve a leading mention against the current live sessions."""
        return resolve_mention(text, {sid: s.title for sid, s in self.sessions.items()}, reserved)

    def _identity(self, sid: str) -> None:
        if sid not in self.sessions:
            msg = f"Unknown session '{sid}'."
            raise MessageError(msg)

    @staticmethod
    def _text(text: str, limit: int) -> int:
        if not isinstance(text, str) or not text.strip():
            msg = "A nonempty message is required."
            raise MessageError(msg)
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError as exc:
            msg = "Invalid Unicode in message."
            raise MessageError(msg) from exc
        if size > limit:
            msg = "Message is too large."
            raise MessageError(msg)
        return size

    def _make_room(self, size: int, *, adding: bool, protect: str = "") -> None:
        if size > self.max_bytes:
            msg = "Message memory budget exceeded."
            raise MessageError(msg)
        while self.retained_bytes + size > self.max_bytes or (
            adding and len(self.messages) >= self.max_messages
        ):
            victim = next(
                (m.id for m in self.messages.values() if m.status in FINAL and m.id != protect),
                None,
            )
            if victim is None:
                reason = "memory" if self.retained_bytes + size > self.max_bytes else "capacity"
                msg = f"Message {reason} limit reached."
                raise MessageError(msg)
            del self.messages[victim]
            self._seen = {key: value for key, value in self._seen.items() if key[1] != victim}

    def send(self, sender: str, target: str, text: str, *, depth: int = 0) -> SessionMessage:
        """Enqueue explicit content with an authoritative sender identity."""
        self._identity(sender)
        if depth > MAX_HOPS or depth < 0:
            msg = "Cross-session request chain limit reached."
            raise MessageError(msg)
        recipient = resolve_target(target, {sid: s.title for sid, s in self.sessions.items()})
        if sender == recipient:
            msg = "Cannot send a message to self."
            raise MessageError(msg)
        size = self._text(text, MAX_TEXT_BYTES)
        if (
            sum(m.recipient == recipient and m.status not in FINAL for m in self.messages.values())
            >= self.max_pending
        ):
            msg = "Recipient message queue is full."
            raise MessageError(msg)
        self._make_room(size, adding=True)
        self._counter += 1
        message = SessionMessage(f"m{self._counter}", sender, recipient, text, depth=depth)
        self.messages[message.id] = message
        return message

    def next_message(self, sid: str) -> SessionMessage | None:
        """Get the oldest queued request for one recipient."""
        return next(
            (m for m in self.messages.values() if m.recipient == sid and m.status == "queued"), None
        )

    def start(self, mid: str) -> None:
        """Mark a queued request delivered exactly once."""
        message = self.messages[mid]
        if message.status != "queued":
            msg = "Message has already been delivered."
            raise MessageError(msg)
        message.status = "running"
        message.version += 1

    def complete(self, mid: str, response: str, status: str = "completed") -> SessionMessage:
        """Finish a request without replacing an already accepted reply."""
        message = self.messages[mid]
        if message.status in FINAL:
            return message
        if status not in FINAL:
            msg = "Invalid completion status."
            raise MessageError(msg)
        size = self._text(response, MAX_REPLY_BYTES)
        self._make_room(size, adding=False, protect=mid)
        message.status, message.response = status, response
        message.version += 1
        return message

    def reply(self, sender: str, mid: str, text: str) -> SessionMessage:
        """Accept a reply only from the running request's recipient."""
        self._identity(sender)
        message = self.messages.get(mid)
        if message is None:
            msg = "Unknown request."
            raise MessageError(msg)
        if message.recipient != sender:
            msg = "Only the recipient can reply to this request."
            raise MessageError(msg)
        if message.status != "running":
            msg = "Request is not running."
            raise MessageError(msg)
        return self.complete(mid, text)

    def read(self, sid: str) -> list[dict]:
        """Return defensive copies of unread requests/results owned by this session."""
        self._identity(sid)
        result = []
        for message in self.messages.values():
            if message.recipient == sid or (message.sender == sid and message.status in FINAL):
                key = (sid, message.id)
                if self._seen.get(key) != message.version:
                    result.append(asdict(message))
                    self._seen[key] = message.version
        return result

    def close(self) -> None:
        """Release all session-derived state."""
        self.messages.clear()
        self.sessions.clear()
        self._seen.clear()
