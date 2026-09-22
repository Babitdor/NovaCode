"""Which Nova session a remote message is for.

With several sessions open (the main one plus ``/session new`` panes), a chat
message has to reach the right one. In order of precedence:

1. ``@name text`` — an explicit address, anywhere.
2. The forum topic it was posted in — each session gets its own topic in a
   Telegram group with topics enabled; the General topic is the main session.
3. The message it replies to — replying to a session's message continues with
   that session (works in a private chat too).
4. ``/use name`` — the chat's default target, else the main session.

Pure bookkeeping, no I/O: the TUI owns the sessions and the bridges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ROOT = "root"

_ADDRESS = re.compile(r"^@([\w.-]+)[,:]?\s+(.+)$", re.S)


@dataclass
class Route:
    sid: str
    text: str
    via: str  # address | topic | reply | default


@dataclass
class RemoteRouter:
    #: (chat_id, sid) -> forum topic id
    topics: dict[tuple[Any, str], int] = field(default_factory=dict)
    #: chat_id -> sid chosen with /use
    default: dict[Any, str] = field(default_factory=dict)

    def topic_of(self, chat_id: Any, sid: str) -> int | None:
        return self.topics.get((chat_id, sid))

    def sid_of_topic(self, chat_id: Any, thread_id: int) -> str | None:
        for (chat, sid), tid in self.topics.items():
            if chat == chat_id and tid == thread_id:
                return sid
        return None

    def forget(self, sid: str) -> list[tuple[Any, int]]:
        """Drop a closed session everywhere; returns its (chat, topic) pairs."""
        gone = [(chat, tid) for (chat, s), tid in self.topics.items() if s == sid]
        for chat, _ in gone:
            self.topics.pop((chat, sid), None)
        for chat in [c for c, s in self.default.items() if s == sid]:
            del self.default[chat]
        return gone

    def resolve(self, msg: Any, sessions: dict[str, str]) -> Route | None:
        """Pick the session for ``msg``. ``sessions`` maps sid -> title.

        None means the message came from a topic whose session is gone.
        """
        text = str(getattr(msg, "text", "") or "")
        chat = getattr(msg, "chat_id", None)
        by_title = {title.lower(): sid for sid, title in sessions.items()}

        m = _ADDRESS.match(text.strip())
        if m and m.group(1).lower() in by_title:
            return Route(by_title[m.group(1).lower()], m.group(2).strip(), "address")

        thread = getattr(msg, "thread_id", None)
        if thread is not None:
            sid = self.sid_of_topic(chat, thread)
            return Route(sid, text, "topic") if sid in sessions else None

        owner = getattr(msg, "reply_to_owner", None)
        if owner in sessions:
            return Route(owner, text, "reply")

        sid = self.default.get(chat, ROOT)
        return Route(sid if sid in sessions else ROOT, text, "default")

    def match(self, name: str, sessions: dict[str, str]) -> str | None:
        """sid of the session called ``name`` (case-insensitive; "main" = root)."""
        name = name.strip().lstrip("@").lower()
        if name in ("main", "root"):
            return ROOT
        return next((sid for sid, title in sessions.items() if title.lower() == name), None)
