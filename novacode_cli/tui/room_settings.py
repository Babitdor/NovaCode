"""Owner controls for a channel, DM or thread."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import on
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from novacode_cli.rooms.store import Record


class RoomSettingsModal(ModalScreen[dict | None]):
    """Gather an explicit owner action; the caller confirms destructive operations."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    RoomSettingsModal { align: center middle; }
    RoomSettingsModal #room-settings-box {
        width: 64; max-width: 95%; height: auto; max-height: 95%;
        overflow-y: auto; border: round $accent; background: $surface; padding: 1 2;
    }
    RoomSettingsModal .settings-title { height: 2; text-style: bold; color: $accent; }
    RoomSettingsModal #channel-topic { height: 4; margin-bottom: 1; }
    RoomSettingsModal .settings-actions { height: auto; }
    RoomSettingsModal .settings-actions Button { width: 1fr; min-width: 8; margin: 0; }
    RoomSettingsModal .settings-note { height: auto; color: $text-muted; }
    """

    def __init__(self, room: Record, *, thread_id: int | None = None) -> None:
        super().__init__()
        self.room = room
        self.thread_id = thread_id

    def compose(self) -> ComposeResult:
        dm = self.room["id"].startswith("dm:")
        with Vertical(id="room-settings-box"):
            yield Static(f"Settings · {self.room['name']}", classes="settings-title", markup=False)
            yield Label("Channel name")
            yield Input(
                self.room["name"].removeprefix("#"), id="channel-name", max_length=40, disabled=dm
            )
            yield Label("Topic (up to 1024 characters)")
            yield TextArea(self.room.get("topic", ""), id="channel-topic", disabled=dm)
            with Horizontal(classes="settings-actions"):
                yield Button("Save", id="channel-save", variant="primary", disabled=dm)
                yield Button("Mark all read", id="channel-mark-read")
            with Horizontal(classes="settings-actions"):
                yield Button(
                    "Clear thread" if self.thread_id else "Clear messages", id="channel-clear"
                )
                yield Button(
                    "Delete channel",
                    id="channel-delete",
                    variant="error",
                    disabled=dm or self.room["id"] == "general" or self.thread_id is not None,
                )
            yield Static(
                "Clear/delete asks for confirmation. Cancel or pause queued/running jobs first. "
                "The default channel and agent DMs cannot be deleted.",
                classes="settings-note",
                markup=False,
            )
            yield Button("Close", id="channel-close")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        action = (event.button.id or "").removeprefix("channel-")
        if action == "close":
            self.dismiss(None)
        else:
            self.dismiss(
                {
                    "action": action,
                    "name": self.query_one("#channel-name", Input).value.strip(),
                    "topic": self.query_one("#channel-topic", TextArea).text,
                }
            )

    def action_cancel(self) -> None:
        self.dismiss(None)
