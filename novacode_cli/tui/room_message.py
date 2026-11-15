"""Attributed Markdown messages and keyboard-accessible room actions."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from novacode_cli.tui.widgets import CachedMarkdown

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from novacode_cli.rooms.store import Record

REACTIONS = ("👍", "❤️", "🎉", "👀")


class RoomMessage(Vertical):
    """Keep message identity stable while edits and reactions refresh in place."""

    def __init__(self, record: Record) -> None:
        super().__init__(id=f"message-{record['id']}", classes="room-message")
        self.record = record

    def compose(self) -> ComposeResult:
        yield Static("", classes="room-message-author", markup=False)
        yield Static("", classes="room-message-reply", markup=False)
        yield Static("", classes="room-message-body")
        with Horizontal(classes="room-message-actions"):
            key = self.record["id"]
            for action, label in (
                ("reply", "Reply"),
                ("thread", "Thread"),
                ("pin", "Pin"),
                ("copy", "Copy"),
            ):
                yield Button(label, id=f"{action}-{key}", compact=True, tooltip=label)
            for index, emoji in enumerate(REACTIONS):
                yield Button(
                    emoji, id=f"react{index}-{key}", compact=True, tooltip=f"React with {emoji}"
                )
            if self.record["author"] == "owner":
                yield Button("Edit", id=f"edit-{key}", compact=True)
            yield Button("Delete", id=f"delete-{key}", compact=True)

    def on_mount(self) -> None:
        self.update_record(self.record)

    def update_record(self, record: Record) -> None:
        """Render untrusted prose as Markdown, with literal attribution metadata."""
        self.record = record
        author = "You" if record["author"] == "owner" else record["author"].title()
        role = " · AGENT" if record["author"] in {"nova", "claude", "codex"} else ""
        timestamp = record.get("created_at") or ""
        flags = (" · edited" if record.get("edited_at") else "") + (
            " · PINNED" if record.get("pinned") else ""
        )
        self.query_one(".room-message-author", Static).update(
            Text(f"{author}{role}  {timestamp.replace('T', ' ')[:19]}{flags}")
        )
        reply = self.query_one(".room-message-reply", Static)
        reply.update(Text(record.get("reply_excerpt", "")))
        reply.display = bool(record.get("reply_to"))
        self.query_one(".room-message-body", Static).update(
            Text("Message deleted", style="dim italic")
            if record.get("deleted")
            else CachedMarkdown(record["text"], hyperlinks=False)
        )
        counts = Counter(r["emoji"] for r in record.get("reactions", []))
        mine = {r["emoji"] for r in record.get("reactions", []) if r["author"] == "owner"}
        for index, emoji in enumerate(REACTIONS):
            button = self.query_one(f"#react{index}-{record['id']}", Button)
            button.label = f"{emoji} {counts[emoji]}" if counts[emoji] else emoji
            button.set_class(emoji in mine, "reacted")
        self.query_one(f"#pin-{record['id']}", Button).label = (
            "Unpin" if record.get("pinned") else "Pin"
        )
        self.query_one(f"#thread-{record['id']}", Button).label = (
            f"Thread {record['thread_count']}" if record.get("thread_count") else "Thread"
        )
        self.query_one(
            f"#thread-{record['id']}", Button
        ).tooltip = f"Open thread · {record.get('thread_count', 0)} replies"
        self.query_one(".room-message-actions").display = not record.get("deleted", False)
