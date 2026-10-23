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

from novacode_cli.sessions.messaging import MessageError, resolve_mention, resolve_target

ROOT = "root"

_ADDRESS = re.compile(r"^@(tab:)?([\w.-]+)[,:]?\s+(.+)$", re.S)


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
        if text.strip().startswith("@tab:"):
            address = resolve_mention(text, sessions)
            if address is not None:
                return Route(address[0], address[1], "address")
        m = _ADDRESS.match(text.strip())
        if m:
            name = m.group(2)
            if m.group(1) or name in sessions or any(title.casefold() == name.casefold() for title in sessions.values()):
                return Route(resolve_target(name, sessions), m.group(3).strip(), "address")

        thread = getattr(msg, "thread_id", None)
        if thread is not None:
            sid = self.sid_of_topic(chat, thread)
            if getattr(getattr(msg, "platform", None), "value", None) == "discord":
                # A Discord gateway restart may recreate a deleted session
                # thread before the TUI refreshes its routing table. The bridge
                # supplies this hint only for its own registered session threads.
                route = getattr(msg, "route", None) or {}
                if "thread_session" in route:
                    sid = route["thread_session"]
            return Route(sid, text, "topic") if sid in sessions else None

        owner = getattr(msg, "reply_to_owner", None)
        if owner in sessions:
            return Route(owner, text, "reply")

        sid = self.default.get(chat, ROOT)
        return Route(sid if sid in sessions else ROOT, text, "default")

    def match(self, name: str, sessions: dict[str, str]) -> str | None:
        """sid of the session called ``name`` (case-insensitive; "main" = root)."""
        name = name.strip().lstrip("@").removeprefix("tab:")
        try:
            return resolve_target(name, sessions)
        except MessageError:
            return None
