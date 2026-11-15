"""Project room workspace with collapsible navigation and an explicit task board."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    Label,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from novacode_cli.rooms import workspace
from novacode_cli.rooms.runtime import RoomRuntime
from novacode_cli.rooms.store import AGENTS, RoomStore, recipients
from novacode_cli.tui.room_message import REACTIONS, RoomMessage
from novacode_cli.tui.widgets import PromptInput

if TYPE_CHECKING:
    from textual.app import ComposeResult


if TYPE_CHECKING:
    from textual.events import Resize


NARROW_WIDTH = 80
COLLAPSE_WIDTH = 65


class RoomMain(Vertical):
    """Reflow controls from the space left after navigation/member panes resize."""

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 75, "toolbar-stacked")
        self.set_class(event.size.width < 50, "toolbar-tiny")
        self.set_class(event.size.width < 30, "toolbar-narrowest")


class RoomsScreen(Screen):
    """Opening/switching rooms never starts a turn; only Send and Start do."""

    BINDINGS: ClassVar = [
        ("escape", "sessions", "Sessions"),
        ("ctrl+backslash", "sidebar", "Channels"),
        ("ctrl+end", "latest", "Latest"),
        ("ctrl+f", "search", "Search"),
    ]
    CSS = """
    RoomsScreen { background: $background; }
    RoomsScreen #rooms-bar { height: 3; background: $surface; }
    RoomsScreen #rooms-title { width: 1fr; padding: 1 1 0 1; text-style: bold; }
    RoomsScreen #rooms-title { text-wrap: nowrap; text-overflow: ellipsis; }
    RoomsScreen #rooms-bar Button { height: 3; min-height: 1; }
    RoomsScreen Button { min-width: 9; margin-right: 1; }
    RoomsScreen #rooms-layout { height: 1fr; }
    RoomsScreen #room-sidebar { width: 24; padding: 0 1; background: $surface; }
    RoomsScreen #room-list { height: 1fr; border: none; }
    RoomsScreen #room-channel { height: 3; }
    RoomsScreen #room-main { width: 1fr; min-width: 0; }
    RoomsScreen #room-heading {
        height: 2; padding: 0 1; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis;
    }
    RoomsScreen #room-toolbar { height: 4; padding: 0 1; border-bottom: solid $surface; }
    RoomsScreen #room-search { width: 1fr; }
    RoomsScreen #room-menu { width: 5; min-width: 5; }
    RoomsScreen #room-toolbar Button { height: 3; min-height: 1; }
    RoomsScreen #room-main.toolbar-stacked #room-toolbar {
        layout: grid; grid-size: 5; grid-columns: 1fr 1fr 1fr 1fr 1fr;
        grid-rows: 3 1; height: 5;
    }
    RoomsScreen #room-main.toolbar-stacked #room-search { column-span: 5; }
    RoomsScreen #room-main.toolbar-stacked #room-toolbar Button {
        width: 1fr; min-width: 0; height: 1; margin: 0; padding: 0;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    RoomsScreen #room-main.toolbar-tiny #room-toolbar {
        grid-size: 3; grid-columns: 1fr 1fr 1fr; grid-rows: 3 1 1; height: 6;
    }
    RoomsScreen #room-main.toolbar-tiny #room-search { column-span: 3; }
    RoomsScreen #room-main.toolbar-narrowest #room-toolbar {
        grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 3 1 1 1; height: 7;
    }
    RoomsScreen #room-main.toolbar-narrowest #room-search { column-span: 2; }
    RoomsScreen #room-topic { height: auto; max-height: 2; padding: 0 1; color: $text-muted; }
    RoomsScreen #room-project { height: 3; padding: 1 0; text-style: bold; }
    RoomsScreen #room-profile { height: 3; padding: 1 0; color: $text-muted; }
    RoomsScreen #room-members { width: 22; padding: 1; background: $surface; }
    RoomsScreen .room-member {
        width: 1fr; height: 2; min-height: 1; margin: 0; padding: 0;
        border: none; background: transparent; color: $text-muted; text-align: left;
    }
    RoomsScreen .room-member:hover, RoomsScreen .room-member:focus {
        color: $accent; background: $accent 15%;
    }
    RoomsScreen #room-member-status { height: auto; color: $text-muted; }
    RoomsScreen #room-context-row { height: 2; background: $primary-muted; }
    RoomsScreen #room-context { width: 1fr; padding-left: 1; }
    RoomsScreen #room-context-cancel { height: 1; min-height: 1; border: none; }
    RoomsScreen #room-result-count { height: 1; padding-left: 2; color: $text-muted; }
    RoomsScreen #room-transcript, RoomsScreen #room-board { height: 1fr; min-height: 1; padding: 0 2; }
    RoomsScreen #room-board { overflow-x: hidden; }
    RoomsScreen .room-message { height: auto; padding: 1; }
    RoomsScreen .room-message-author { height: 1; color: $accent; text-style: bold; }
    RoomsScreen .room-message-reply { height: auto; color: $text-muted; }
    RoomsScreen .room-message-body { height: auto; padding: 0; }
    RoomsScreen .room-message-actions {
        height: auto; layout: grid; grid-size: 10;
        grid-columns: 9 12 7 7 6 6 6 6 8 9; grid-rows: 1;
    }
    RoomsScreen .room-message-actions Button {
        height: 1; min-height: 1; min-width: 3; width: auto;
        border: none; padding: 0 1; margin: 0 1 0 0;
        text-wrap: nowrap; text-overflow: ellipsis;
        background: transparent; color: $text-muted;
    }
    RoomsScreen .room-message-actions Button:focus,
    RoomsScreen .room-message-actions Button.reacted { background: $accent 15%; color: $accent; }
    RoomsScreen .task { height: auto; padding: 1 0; border-top: solid $primary-muted; }
    RoomsScreen .task-summary, RoomsScreen .room-note { height: auto; }
    RoomsScreen .task-actions { height: auto; layout: horizontal; }
    RoomsScreen .task-actions Button { min-width: 7; height: 3; margin-right: 0; }
    RoomsScreen #room-goal { height: 5; margin: 1 0; }
    RoomsScreen #room-board Input { margin-bottom: 1; }
    RoomsScreen #room-status { height: auto; max-height: 5; padding: 0 1; color: $warning; }
    RoomsScreen #room-compose-row { height: auto; min-height: 3; max-height: 9; padding: 0 1; }
    RoomsScreen #room-composer { width: 1fr; height: auto; min-height: 3; max-height: 9; border: tall $surface; }
    RoomsScreen #room-suggestions { height: auto; max-height: 7; display: none; }
    RoomsScreen #room-hint { height: 2; padding: 0 1; color: $text-muted; }
    RoomsScreen.narrow #room-sidebar { width: 19; }
    RoomsScreen.narrow .task-actions { layout: vertical; }
    RoomsScreen.compact-messages .room-message-actions {
        grid-size: 5; grid-columns: 1fr 1fr 1fr 1fr 1fr;
    }
    RoomsScreen.rooms-short #rooms-bar { height: 1; }
    RoomsScreen.rooms-short #rooms-bar Button { height: 1; }
    RoomsScreen.rooms-short #rooms-title { padding: 0 1; }
    RoomsScreen.rooms-short #room-heading { height: 1; }
    RoomsScreen.rooms-short #room-topic, RoomsScreen.rooms-short #room-hint { display: none; }
    RoomsScreen.rooms-short #room-context-row { height: 1; }
    RoomsScreen.rooms-short #room-status { max-height: 1; }
    RoomsScreen.rooms-short #room-compose-row, RoomsScreen.rooms-short #room-composer {
        max-height: 3;
    }
    RoomsScreen.rooms-short #room-result-count { height: 0; }
    RoomsScreen.rooms-short #room-main.toolbar-stacked #room-search {
        height: 1; min-height: 1; border: none; padding: 0 1;
    }
    RoomsScreen.rooms-short #room-main.toolbar-stacked #room-toolbar {
        grid-rows: 1 1; height: 3;
    }
    RoomsScreen.rooms-short #room-main.toolbar-tiny #room-toolbar {
        grid-rows: 1 1 1; height: 4;
    }
    RoomsScreen.rooms-short #room-main.toolbar-narrowest #room-toolbar {
        grid-rows: 1 1 1 1; height: 5;
    }
    """

    def __init__(self, project: Path, viewer: str = "owner") -> None:
        """Retain a per-session draft identity independently from model identities."""
        super().__init__()
        self.project = project
        self.viewer = viewer
        self.runtime: RoomRuntime | None = None
        self.room = "general"
        self.channel_label = "#general"
        self.board = False
        self.last_message = 0
        self.snapshot = ""
        self.navigation = ""
        self.selected_notes: set[str] = set()
        self.completions: list[str] = []
        self.completion_value = ""
        self.updating = False
        self.refreshing = False
        self.runtime_error = ""
        self.leaving = False
        self.loaded = False
        self.thread_id: int | None = None
        self.reply_to: int | None = None
        self.editing: int | None = None
        self.editing_draft = ""
        self.pins_only = False
        self.message_widgets: dict[int, RoomMessage] = {}
        self.sending = False
        self.navigation_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        """Keep navigation, transcript and composer as separate focus/scroll regions."""
        with Horizontal(id="rooms-bar"):
            yield Button("Channels", id="rooms-toggle", compact=True)
            yield Static(Text(f"{self.project.name} / Rooms"), id="rooms-title")
            yield Button("Sessions", id="rooms-sessions", compact=True)
        with Horizontal(id="rooms-layout"):
            with Vertical(id="room-sidebar"):
                yield Static("PROJECT CHANNELS", id="room-project")
                yield OptionList(id="room-list")
                yield Input(placeholder="+ Create channel", id="room-channel")
                yield Static("● You · Project owner", id="room-profile")
            with RoomMain(id="room-main"):
                yield Static("#general", id="room-heading", markup=False)
                yield Static("", id="room-topic", markup=False)
                with Horizontal(id="room-toolbar"):
                    yield Input(
                        placeholder="Search messages in this conversation", id="room-search"
                    )
                    yield Button("Pins", id="room-pins", compact=True)
                    yield Button("Members", id="room-members-toggle", compact=True)
                    yield Button("Tasks", id="room-tasks", compact=True)
                    yield Button(
                        "⋯",
                        id="room-menu",
                        tooltip="Channel settings and message controls",
                        compact=True,
                    )
                    yield Button("Channel", id="room-thread-back", compact=True)
                yield Static("", id="room-result-count", markup=False)
                yield VerticalScroll(id="room-transcript")
                yield VerticalScroll(id="room-board")
                yield Static("", id="room-status", markup=False)
                yield OptionList(id="room-suggestions")
                with Horizontal(id="room-context-row"):
                    yield Static("", id="room-context", markup=False)
                    yield Button("Cancel", id="room-context-cancel", compact=True)
                with Horizontal(id="room-compose-row"):
                    yield PromptInput(placeholder="Message #general…", id="room-composer")
                    yield Button("Send", id="room-send", variant="primary")
                yield Static(
                    "@agent replies read-only · Shared board → review scope → Start",
                    id="room-hint",
                    markup=False,
                )
            with VerticalScroll(id="room-members"):
                yield Label("MEMBERS")
                yield Static("● You · owner", classes="room-note")
                for agent in AGENTS:
                    yield Button(f"○ {agent.title()}", id=f"member-{agent}", classes="room-member")
                yield Static(
                    "Idle agents run only when you send a message or start a task.",
                    id="room-member-status",
                )

    async def on_mount(self) -> None:
        """Start an idle foreground runtime, with no model calls."""
        self.layout_width(self.size.width)
        self.set_class(self.size.height < 24, "rooms-short")
        self.query_one("#room-board").display = False
        self.query_one("#room-context-row").display = False
        self.query_one("#room-result-count").display = False
        try:
            store = await asyncio.to_thread(RoomStore, self.project)
            self.runtime = RoomRuntime(store)
            await self.runtime.open()
            await self.switch("general")
            self.set_interval(0.5, self.poll)
        except Exception as exc:  # noqa: BLE001 - Initialization errors must be visible.
            self.status(str(exc))

    def on_resize(self, event: Resize) -> None:
        """Collapse navigation initially on narrow terminals; keep a toggle available."""
        self.layout_width(event.size.width)
        self.set_class(event.size.height < 24, "rooms-short")
        topic = self.query_one("#room-topic", Static)
        topic.display = event.size.height >= 24 and not self.board and bool(topic.content)

    def layout_width(self, width: int) -> None:
        """Apply responsive rules before the first messages mount as well as on resize."""
        self.set_class(width < NARROW_WIDTH, "narrow")
        self.set_class(width < 140, "compact-messages")
        self.query_one("#room-sidebar").display = width >= COLLAPSE_WIDTH
        self.query_one("#room-members").display = width >= 110

    def status(self, text: str) -> None:
        """Display actionable setup, approval and persistence failures."""
        self.query_one("#room-status", Static).update(text)

    @property
    def draft_viewer(self) -> str:
        """Thread drafts stay separate while retaining the room's access boundary."""
        return f"{self.viewer}:thread:{self.thread_id}" if self.thread_id else self.viewer

    async def switch(self, room: str, thread_id: int | None = None) -> None:
        """Serialize navigation with transcript refreshes so rooms cannot mix."""
        async with self.navigation_lock:
            await self._switch(room, thread_id)
        await self.poll()

    async def _switch(self, room: str, thread_id: int | None) -> None:
        """Persist the old draft before reading the selected room."""
        if not self.runtime:
            return
        composer = self.query_one("#room-composer", PromptInput)
        if self.loaded and not self.board:
            await self.runtime.db(
                "view",
                self.draft_viewer,
                self.room,
                self.editing_draft if self.editing else composer.value,
            )
        self.completions = []
        self.completion_value = ""
        self.query_one("#room-suggestions").display = False
        self.board = room == "__board"
        self.thread_id = thread_id
        self.reply_to = self.editing = None
        self.query_one("#room-context-row").display = False
        self.pins_only = False
        self.query_one("#room-pins", Button).variant = "default"
        self.query_one("#room-search", Input).value = ""
        if not self.board:
            self.room = room
            if thread_id is not None:
                await self.runtime.db("message_record", room, thread_id, include_deleted=True)
            view = await self.runtime.db("view", self.draft_viewer, room, seen=True)
            self.updating = True
            composer.value = view["draft"]
            self.updating = False
            channel = await self.runtime.db("room", room)
            composer.placeholder = f"Message {channel['name']}…"
            self.loaded = True
            self.last_message = 0
            await self.query_one("#room-transcript").remove_children()
            self.message_widgets.clear()
        self.query_one("#room-transcript").display = not self.board
        self.query_one("#room-board").display = self.board
        self.query_one("#room-compose-row").display = not self.board
        self.query_one("#room-toolbar").display = not self.board
        self.query_one("#room-thread-back").display = thread_id is not None
        self.query_one("#room-topic").display = not self.board
        self.query_one("#room-heading", Static).update(
            "Shared board"
            if self.board
            else (self.room[3:].title() if self.room.startswith("dm:") else "#" + self.room)
            + (f" / Thread #{self.thread_id}" if self.thread_id else "")
        )
        self.snapshot = ""
        self.navigation = ""
        if not self.board:
            composer.focus()

    async def poll(self) -> None:
        """Append messages without pulling a reader away from their scroll position."""
        if not self.runtime or self.refreshing or self.leaving:
            return
        async with self.navigation_lock:
            if not self.runtime or self.leaving:
                return
            self.refreshing = True
            try:
                if self.runtime.error != self.runtime_error:
                    self.runtime_error = self.runtime.error
                    self.status(self.runtime_error)
                rooms = await self.runtime.db("rows", "SELECT * FROM rooms ORDER BY rowid")
                if self.room not in {room["id"] for room in rooms}:
                    self.loaded = False
                    await self._switch("general", None)
                    self.status("The channel was deleted. Switched to the default channel.")
                current = next(room for room in rooms if room["id"] == self.room)
                self.channel_label = current["name"]
                if not self.board:
                    self.query_one("#room-heading", Static).update(
                        Text(
                            current["name"]
                            + (f" / Thread #{self.thread_id}" if self.thread_id else "")
                        )
                    )
                    self.query_one("#room-topic", Static).update(Text(current["topic"]))
                    self.query_one("#room-topic").display = (
                        bool(current["topic"]) and self.size.height >= 24
                    )
                    self.query_one(
                        "#room-composer", PromptInput
                    ).placeholder = f"Message {current['name']}…"
                options = []
                for room in rooms:
                    view = await self.runtime.db("view", self.viewer, room["id"])
                    count = await self.runtime.db(
                        "rows",
                        "SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>?",
                        (room["id"], view["seen"]),
                    )
                    suffix = f" ({count[0]['n']})" if count[0]["n"] else ""
                    label = (
                        ("DM · " if room["id"].startswith("dm:") else "") + room["name"] + suffix
                    )
                    options.append((room["id"], label))
                nav = json.dumps(options)
                if nav != self.navigation:
                    self.navigation = nav
                    listing = self.query_one("#room-list", OptionList)
                    listing.clear_options()
                    listing.add_options(
                        [Option("TEXT CHANNELS", disabled=True)]
                        + [
                            Option(Text(label), id=key)
                            for key, label in options
                            if not key.startswith("dm:")
                        ]
                        + [Option("DIRECT MESSAGES", disabled=True)]
                        + [
                            Option(Text(label), id=key)
                            for key, label in options
                            if key.startswith("dm:")
                        ]
                        + [
                            Option("WORKSPACE", disabled=True),
                            Option("▦ Shared task board", id="__board"),
                        ]
                    )
                    selected = "__board" if self.board else self.room
                    listing.highlighted = listing.get_option_index(selected)
                    listing.scroll_to_highlight()
                if self.board:
                    await self.render_board()
                else:
                    messages = await self.runtime.db("history", self.room)
                    scroll = self.query_one("#room-transcript", VerticalScroll)
                    follow = scroll.is_vertical_scroll_end
                    by_id = {message["id"]: message for message in messages}
                    if self.thread_id is not None and self.thread_id not in by_id:
                        self.loaded = False
                        await self._switch(self.room, None)
                    for key in set(self.message_widgets) - by_id.keys():
                        await self.message_widgets.pop(key).remove()
                    thread_counts = Counter(message.get("thread_id") for message in messages)
                    for message in messages:
                        message["thread_count"] = thread_counts[message["id"]]
                        if message.get("reply_to"):
                            parent = by_id.get(message["reply_to"], {})
                            excerpt = (
                                "Message deleted"
                                if parent.get("deleted")
                                else parent.get("text", "Unavailable")
                            )
                            message["reply_excerpt"] = (
                                f"↳ {parent.get('author', 'message')}: {' '.join(excerpt.split())[:100]}"
                            )
                        key = message["id"]
                        if key not in self.message_widgets:
                            widget = RoomMessage(message)
                            self.message_widgets[key] = widget
                            await scroll.mount(widget)
                        elif self.message_widgets[key].record != message:
                            self.message_widgets[key].update_record(message)
                        self.last_message = max(self.last_message, key)
                    self.filter_messages()
                    if follow:
                        scroll.scroll_end(animate=False)
                        await self.runtime.db("view", self.draft_viewer, self.room, seen=True)
                jobs = await self.runtime.db(
                    "rows",
                    "SELECT * FROM jobs WHERE status IN ('working','queued','cancelling','blocked','paused') "
                    "ORDER BY CASE status WHEN 'working' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, rowid DESC",
                )
                self.update_members(jobs)
                self.query_one("#room-hint", Static).update(
                    " · ".join(f"{j['agent'].title()}: {j['status']}" for j in jobs)
                    or "Enter to send · Shift+Enter for newline · @agent / @file · Ctrl+F search"
                )
            except Exception as exc:  # noqa: BLE001 - Poll errors must leave the composer usable.
                self.status(str(exc))
            finally:
                self.refreshing = False

    def update_members(self, jobs: list[dict]) -> None:
        """Report actual scheduler activity, rather than pretend online presence."""
        for agent in AGENTS:
            active = next((job for job in jobs if job["agent"] == agent), None)
            state = active["status"] if active else "idle"
            marker = "●" if state == "working" else "○"
            self.query_one(f"#member-{agent}", Button).label = f"{marker} {agent.title()} · {state}"

    @on(Input.Changed, "#room-search")
    def search_changed(self, event: Input.Changed) -> None:
        """Filter local history without dispatching a model or changing drafts."""
        event.stop()
        self.filter_messages()

    def filter_messages(self) -> None:
        """Keep channel messages, thread messages, pins and text search composable."""
        query = self.query_one("#room-search", Input).value.casefold().strip()
        shown = 0
        for key, widget in self.message_widgets.items():
            record = widget.record
            in_scope = record.get("thread_id") == self.thread_id or (
                self.thread_id is not None and key == self.thread_id
            )
            matches = not query or (
                not record.get("deleted")
                and query in f"{record['author']} {record['text']}".casefold()
            )
            widget.display = (
                in_scope and matches and (not self.pins_only or bool(record.get("pinned")))
            )
            shown += int(widget.display)
        label = self.query_one("#room-result-count", Static)
        label.display = not self.board
        label.update(
            f"{shown} message{'s' if shown != 1 else ''}"
            + (" · pinned" if self.pins_only else "")
            + (" · search results" if query else "")
            if shown
            else "No matching messages"
            if query or self.pins_only
            else "Start the conversation. Mention @nova, @claude or @codex to collaborate."
        )

    def action_search(self) -> None:
        """Focus search without interrupting the current message draft."""
        if not self.board:
            self.query_one("#room-search", Input).focus()

    async def cancel_context(self) -> None:
        """Restore the unsent draft when an edit is dismissed."""
        if self.editing:
            self.query_one("#room-composer", PromptInput).value = self.editing_draft
        self.reply_to = self.editing = None
        self.query_one("#room-context-row").display = False

    async def message_action(self, key: str) -> None:
        """Bind message controls to a room-scoped stored identity."""
        if not self.runtime:
            return
        action, value = key.split("-", 1)
        message_id = int(value)
        message = await self.runtime.db("message_record", self.room, message_id)
        composer = self.query_one("#room-composer", PromptInput)
        if action == "thread":
            await self.switch(self.room, message.get("thread_id") or message_id)
        elif action in {"reply", "edit"}:
            await self.cancel_context()
            if action == "edit":
                if message["author"] != "owner":
                    raise ValueError("Only your own messages can be edited")
                self.editing = message_id
                self.editing_draft = composer.value
                composer.value = message["text"]
            else:
                self.reply_to = message_id
            self.query_one("#room-context", Static).update(
                Text(
                    f"{'Editing your message' if self.editing else 'Replying to ' + message['author']} · #{message_id}"
                )
            )
            self.query_one("#room-context-row").display = True
            composer.focus()
        elif action == "pin":
            await self.runtime.db("change_message", self.room, message_id, "pin")
        elif action == "copy":
            self.app.copy_to_clipboard(message["text"])
            self.notify("Message copied")
        elif action == "delete":
            from novacode_cli.tui.screens import ConfirmModal

            approved = await self.app.push_screen_wait(
                ConfirmModal(
                    "Delete message?",
                    Text("This removes its text and reactions. Replies are preserved."),
                )
            )
            if approved:
                await self.runtime.db("change_message", self.room, message_id, "delete")
        elif action.startswith("react"):
            await self.runtime.db(
                "react", self.room, message_id, "owner", REACTIONS[int(action[5:])]
            )
        await self.poll()

    async def render_board(self) -> None:  # noqa: PLR0912, PLR0915 - Declarative board with preserved form state.
        """Task cards expose scope, limits and separate integration approval."""
        if not self.runtime:
            return
        tasks = await self.runtime.db("rows", "SELECT body FROM tasks ORDER BY rowid DESC")
        notes = await self.runtime.db("rows", "SELECT * FROM notes ORDER BY rowid DESC")
        jobs = await self.runtime.db("rows", "SELECT * FROM jobs ORDER BY rowid DESC LIMIT 30")
        snapshot = json.dumps([tasks, notes, jobs])
        if snapshot == self.snapshot:
            return
        # Keep partially entered owner proposals/export destinations during updates.
        board = self.query_one("#room-board", VerticalScroll)
        goal = self.query_one("#room-goal", TextArea).text if board.query("#room-goal") else ""
        participants = (
            self.query_one("#room-participants", Input).value
            if board.query("#room-participants")
            else ",".join(recipients("", self.room))
        )
        destination = (
            self.query_one("#room-export", Input).value if board.query("#room-export") else ""
        )
        position = board.scroll_y
        focused = self.app.focused
        focus_id = focused.id if focused else None
        cursor = focused.cursor_position if isinstance(focused, Input) else 0
        selection = focused.selection if isinstance(focused, TextArea) else None
        await board.remove_children()
        await board.mount(
            Label(f"New task in {self.channel_label} · Goal"),
            TextArea(goal, id="room-goal"),
            Label("Participants (comma-separated)"),
            Input(participants, placeholder="nova,claude,codex", id="room-participants"),
            Button("Review task scope", id="room-propose", variant="primary"),
            Static(
                "Read / edit / test in an isolated worktree. Two workers; six turns per task. "
                "Worktrees are not security sandboxes. Native CLI approvals still apply.",
                markup=False,
            ),
        )
        for record in tasks:
            task = json.loads(record["body"])
            card = Vertical(classes="task")
            await board.mount(card)
            await card.mount(
                Static(
                    Text(
                        f"{task['goal']}\n{task['status']} · "
                        f"{task['limit'] - task['turns']}/{task['limit']} turns left\n"
                        f"Participants: {', '.join(task['participants'])}\n"
                        f"Operations: {', '.join(task['operations'])}\n"
                        f"Base: {task['base'] or 'Git required'}\n"
                        f"Worktree: {task['worktree'] or 'not prepared'}\n"
                        "Uncommitted project changes are not copied.\n"
                        f"{task['warning']}\n{task['error']}"
                    ),
                    classes="task-summary",
                )
            )
            controls = Horizontal(classes="task-actions")
            await card.mount(controls)
            if task["status"] == "awaiting approval" and task["worktree"]:
                await controls.mount(Button("Start", id="start-" + task["id"], variant="primary"))
            if task["worktree"]:
                await controls.mount(Button("View diff", id="diff-" + task["id"]))
            if task["status"] == "finished":
                await controls.mount(
                    Button("Claude review", id="review-" + task["id"]),
                    Button("Integrate", id="integrate-" + task["id"]),
                )
        for job in jobs:
            usage = (
                json.loads(job["usage"])
                if job["usage"]
                else "unknown (provider did not report usage)"
            )
            detail = Static(Text(f"{job['status']}\nUsage: {usage}\n{job['detail']}"))
            group = Collapsible(
                detail,
                title=f"{job['agent'].title()} · {job['status']} · {job['id'][:8]}",
                collapsed=True,
            )
            await board.mount(group)
            controls = Horizontal(classes="task-actions")
            await group.mount(controls)
            if job["status"] in {"working", "queued"} and job["runtime"] == self.runtime.id:
                await controls.mount(Button("Cancel", id="cancel-" + job["id"]))
            if job["status"] in {"paused", "blocked"}:
                await controls.mount(
                    Button("Resume", id="resume-" + job["id"]),
                    Button("Extend +6", id="extend-" + job["id"]),
                )
        await board.mount(Label("Published decisions, findings and handoffs"))
        for note in notes:
            await board.mount(
                Static(
                    Text(f"{note['kind']} · {note['author']} · {note['room']}\n{note['body']}"),
                    classes="room-note",
                )
            )
            if not note["room"].startswith("dm:"):
                await board.mount(
                    Checkbox(
                        "Export this note",
                        value=note["id"] in self.selected_notes,
                        id="note-" + note["id"],
                    )
                )
        await board.mount(
            Input(
                destination, placeholder="Export selected notes to a new .md path", id="room-export"
            ),
            Button("Export Markdown", id="room-export-button"),
        )
        board.scroll_to(y=position, animate=False)
        if focus_id in {"room-participants", "room-export"}:
            field = self.query_one("#" + focus_id, Input)
            field.focus()
            field.cursor_position = cursor
        elif focus_id == "room-goal" and selection:
            goal_field = self.query_one("#room-goal", TextArea)
            goal_field.focus()
            goal_field.selection = selection
        self.snapshot = snapshot

    @on(OptionList.OptionSelected, "#room-list")
    async def selected(self, event: OptionList.OptionSelected) -> None:
        """Switch to the selected channel, DM or board."""
        if event.option.id:
            await self.switch(event.option.id)

    @on(TextArea.Changed, "#room-composer")
    async def draft_changed(self, event: TextArea.Changed) -> None:
        """Persist the current draft and distinguish agent and file completions."""
        event.stop()
        value = event.text_area.text
        if (
            not self.runtime
            or self.board
            or self.updating
            or self.editing
            or value != self.query_one("#room-composer", PromptInput).value
        ):
            return
        runtime, room, thread_id = self.runtime, self.room, self.thread_id
        viewer = self.draft_viewer
        listing = self.query_one("#room-suggestions", OptionList)
        listing.display = False
        listing.clear_options()
        self.completions = []
        self.completion_value = ""
        await runtime.db("view", viewer, room, value)
        match = re.search(r"(?:^|\s)@([^\s]*)$", value)
        suggestions: list[tuple[str, str]] = []
        if match:
            partial = match[1]
            suggestions = [
                (agent, f"Agent · @{agent}") for agent in AGENTS if agent.startswith(partial)
            ]

            def files() -> list[str]:
                parent = workspace_path = runtime.store.project
                if "/" in partial:
                    parent = workspace_path / partial.rsplit("/", 1)[0]
                if not parent.resolve().is_relative_to(workspace_path) or not parent.is_dir():
                    return []
                return [
                    str(p.relative_to(workspace_path)).replace("\\", "/")
                    for p in parent.iterdir()
                    if str(p.relative_to(workspace_path)).replace("\\", "/").startswith(partial)
                ][:20]

            suggestions.extend((name, f"File · @{name}") for name in await asyncio.to_thread(files))
        # Threaded persistence/listing may finish after another keystroke or navigation.
        if (
            self.board
            or self.leaving
            or self.room != room
            or self.thread_id != thread_id
            or value != self.query_one("#room-composer", PromptInput).value
        ):
            return
        self.completions = [name for name, _ in suggestions]
        self.completion_value = value
        listing.add_options([Option(Text(label)) for _, label in suggestions])
        listing.display = bool(suggestions)

    @on(OptionList.OptionSelected, "#room-suggestions")
    def complete(self, event: OptionList.OptionSelected) -> None:
        """Insert the chosen mention without consuming file syntax."""
        composer = self.query_one("#room-composer", PromptInput)
        if (
            self.board
            or composer.value != self.completion_value
            or not 0 <= event.option_index < len(self.completions)
        ):
            return
        name = self.completions[event.option_index]
        value = '@"' + name + '"' if " " in name else "@" + name
        composer.value = re.sub(r"@[^\s]*$", lambda _: value + " ", composer.value)
        composer.cursor_position = len(composer.value)
        composer.focus()

    @on(Checkbox.Changed)
    def note_selected(self, event: Checkbox.Changed) -> None:
        """Retain explicit note selections across board refreshes."""
        key = (event.checkbox.id or "").removeprefix("note-")
        if event.value:
            self.selected_notes.add(key)
        else:
            self.selected_notes.discard(key)

    @on(Input.Submitted)
    @on(PromptInput.Submitted)
    async def submitted(self, event: Input.Submitted | PromptInput.Submitted) -> None:
        """Handle composer submission and owner-created channels."""
        event.stop()
        if not self.runtime:
            return
        try:
            if event.input.id == "room-channel":
                room = await self.runtime.db("create_channel", event.value.strip())
                event.input.value = ""
                await self.switch(room)
            elif event.input.id == "room-composer":
                await self.send()
        except (ValueError, OSError) as exc:
            self.status(str(exc))

    async def send(self) -> None:
        """Dispatch one explicit message or save an owner edit without running an agent."""
        composer = self.query_one("#room-composer", PromptInput)
        if not composer.value.strip() or not self.runtime or self.sending:
            return
        self.sending = True
        room, thread_id, value = self.room, self.thread_id, composer.value
        viewer, editing, original_draft = self.draft_viewer, self.editing, self.editing_draft
        try:
            if editing:
                await self.runtime.db("change_message", room, editing, "edit", value)
                if self.room == room and self.thread_id == thread_id and self.editing == editing:
                    composer.value = original_draft
            else:
                await self.runtime.send(room, value, reply_to=self.reply_to, thread_id=thread_id)
                composer.remember(value)
                if self.room == room and self.thread_id == thread_id and composer.value == value:
                    composer.value = ""
                await self.runtime.db("clear_draft", viewer, room, value)
            if self.room == room and self.thread_id == thread_id:
                self.reply_to = self.editing = None
                self.query_one("#room-context-row").display = False
            self.status("")
            await self.poll()
        finally:
            self.sending = False

    @on(Button.Pressed)
    @work(group="room-actions", exclusive=False)
    async def pressed(self, event: Button.Pressed) -> None:
        """Keep slow Git operations out of event handlers and double starts fenced in SQLite."""
        key = event.button.id or ""
        event.button.disabled = True
        try:
            if key == "rooms-toggle":
                self.action_sidebar()
            elif key == "rooms-sessions":
                await self.action_sessions()
            elif key == "room-send":
                await self.send()
            elif key == "room-members-toggle":
                panel = self.query_one("#room-members")
                panel.display = not panel.display
                if panel.display and self.size.width < 100:
                    self.query_one("#room-sidebar").display = False
            elif key == "room-menu":
                await self.manage_channel()
            elif key == "room-tasks":
                await self.switch("__board")
            elif key == "room-thread-back":
                await self.switch(self.room)
            elif key == "room-pins":
                self.pins_only = not self.pins_only
                event.button.variant = "primary" if self.pins_only else "default"
                self.filter_messages()
            elif key == "room-context-cancel":
                await self.cancel_context()
            elif key.startswith("member-"):
                await self.switch("dm:" + key[7:])
            elif key.startswith(
                ("reply-", "thread-", "pin-", "copy-", "edit-", "delete-", "react")
            ):
                await self.message_action(key)
            elif self.runtime:
                await self.owner_action(key)
        except Exception as exc:  # noqa: BLE001 - Owner operations surface actionable failures.
            self.status(str(exc))
        finally:
            if event.button.is_mounted:
                event.button.disabled = False

    async def manage_channel(self) -> None:
        """Apply settings to the conversation displayed when the menu opened."""
        if not self.runtime:
            return
        from novacode_cli.tui.room_settings import RoomSettingsModal
        from novacode_cli.tui.screens import ConfirmModal

        room, thread_id = self.room, self.thread_id
        channel = await self.runtime.db("room", room)
        choice = await self.app.push_screen_wait(RoomSettingsModal(channel, thread_id=thread_id))
        if not choice:
            return
        action = choice["action"]
        if action == "save":
            await self.runtime.db("update_channel", room, choice["name"], choice["topic"])
            self.notify("Channel settings saved")
        elif action == "mark-read":
            await self.runtime.db("mark_read", self.viewer)
            self.notify("All channels and DMs marked read")
        elif action in {"clear", "delete"}:
            target = f"thread #{thread_id} in {channel['name']}" if thread_id else channel["name"]
            deleting = action == "delete"
            approved = await self.app.push_screen_wait(
                ConfirmModal(
                    f"{'Delete channel' if deleting else 'Clear messages'} · {target}?",
                    Text(
                        "Deletes this channel, all messages/threads, notes, drafts and task/job history. "
                        "Project files and Git worktrees are preserved. This cannot be undone."
                        if deleting
                        else "Removes all messages, pins and reactions in this conversation and resets its "
                        "agent discussion sessions. Clearing a channel also removes its threads. "
                        "Tasks, notes and files are preserved. This cannot be undone."
                    ),
                )
            )
            if not approved:
                return
            async with self.navigation_lock:
                if deleting:
                    await self.runtime.db("delete_channel", room)
                    if self.room == room:
                        self.loaded = False
                        await self._switch("general", None)
                    self.notify("Channel deleted; project files and worktrees preserved")
                else:
                    removed = await self.runtime.db("clear_messages", room, thread_id)
                    if self.room == room and self.thread_id == thread_id:
                        await self.cancel_context()
                        await self._switch(room, thread_id)
                    self.notify(f"Cleared {removed} messages")
        self.navigation = ""
        self.snapshot = ""
        await self.poll()

    async def owner_action(self, key: str) -> None:
        """Apply an explicit owner decision from a task card or note selection."""
        if not self.runtime:
            return
        if key == "room-propose":
            goal = self.query_one("#room-goal", TextArea).text
            participants = [
                p.strip().lower()
                for p in self.query_one("#room-participants", Input).value.split(",")
            ]
            task = await self.runtime.db("propose", self.room, goal, participants)
            await asyncio.to_thread(workspace.prepare, self.runtime.store, task["id"])
        elif key.startswith("start-"):
            await self.runtime.start(key[6:])
        elif key.startswith("diff-"):
            from novacode_cli.tui.screens import InfoListScreen

            content = await asyncio.to_thread(workspace.diff, self.runtime.store, key[5:])
            self.app.push_screen(
                InfoListScreen("Task diff", [Text(line).markup for line in content.splitlines()])
            )
        elif key.startswith("integrate-"):
            from novacode_cli.tui.screens import ConfirmModal

            task = await self.runtime.db("task", key[10:])
            approved = await self.app.push_screen_wait(
                ConfirmModal(
                    "Integrate task?",
                    Text(
                        f"{task['goal']}\nCommit the worktree's changes "
                        "and fast-forward this project.\n"
                        "Review View diff first. Dirty or changed project bases are refused."
                    ),
                )
            )
            if approved:
                await asyncio.to_thread(workspace.integrate, self.runtime.store, task["id"])
        elif key.startswith("review-"):
            task = await self.runtime.db("task", key[7:])
            if "claude" not in task["participants"]:
                msg = "Claude was not approved for this task; create a review task including Claude"
                raise ValueError(msg)
            await self.runtime.db(
                "enqueue",
                task["room"],
                "claude",
                "Review this task worktree read-only. "
                "Report findings, files and checks. Do not edit.",
                self.runtime.id,
                task["id"],
                readonly=True,
            )
        elif key.startswith("cancel-"):
            await self.runtime.cancel(key[7:])
        elif key.startswith(("resume-", "extend-")):
            from novacode_cli.tui.screens import ConfirmModal

            approved = await self.app.push_screen_wait(
                ConfirmModal(
                    "Resume interrupted work?",
                    Text(
                        "Inspect the preserved worktree before resuming. "
                        "Actions may already have run.\n"
                        + (
                            "Grant six additional turns within the same scope."
                            if key.startswith("extend-")
                            else "Resume within the remaining approved allowance."
                        )
                    ),
                )
            )
            if approved:
                await self.runtime.resume(key[7:], extend=6 if key.startswith("extend-") else 0)
        elif key == "room-export-button":
            value = self.query_one("#room-export", Input).value
            target = await asyncio.to_thread(lambda: Path(value).expanduser())
            await self.runtime.db("export", sorted(self.selected_notes), target)
            self.status(f"Exported selected channel notes to {target}")
        await self.poll()

    def action_sidebar(self) -> None:
        """Toggle navigation independently of the transcript."""
        sidebar = self.query_one("#room-sidebar")
        sidebar.display = not sidebar.display
        if sidebar.display and self.size.width < 100:
            self.query_one("#room-members").display = False

    def action_latest(self) -> None:
        """Jump to the latest message only on request."""
        self.query_one("#room-transcript", VerticalScroll).scroll_end(animate=False)

    async def action_sessions(self) -> None:
        """Return to existing session tabs; pause room execution before leaving."""
        self.leaving = True
        if self.runtime:
            if not self.board:
                await self.runtime.db(
                    "view",
                    self.draft_viewer,
                    self.room,
                    self.editing_draft
                    if self.editing
                    else self.query_one("#room-composer", PromptInput).value,
                )
            await self.runtime.close()
            self.runtime = None
        self.app.pop_screen()

    async def on_unmount(self) -> None:
        """Stop room workers when the app closes or removes this screen."""
        if self.runtime:
            await self.runtime.close()
            self.runtime = None
