"""Project room workspace with collapsible navigation and an explicit task board."""

from __future__ import annotations

import asyncio
import json
import re
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

if TYPE_CHECKING:
    from textual.app import ComposeResult


if TYPE_CHECKING:
    from textual.events import Resize


NARROW_WIDTH = 80
COLLAPSE_WIDTH = 65


class RoomsScreen(Screen):
    """Opening/switching rooms never starts a turn; only Send and Start do."""

    BINDINGS: ClassVar = [
        ("escape", "sessions", "Sessions"),
        ("ctrl+backslash", "sidebar", "Channels"),
        ("ctrl+end", "latest", "Latest"),
    ]
    CSS = """
    RoomsScreen { background: $background; }
    RoomsScreen #rooms-bar { height: 3; background: $surface; }
    RoomsScreen #rooms-title { width: 1fr; padding: 1 1 0 1; text-style: bold; }
    RoomsScreen Button { min-width: 9; margin-right: 1; }
    RoomsScreen #rooms-layout { height: 1fr; }
    RoomsScreen #room-sidebar { width: 24; padding: 0 1; background: $surface; }
    RoomsScreen #room-list { height: 1fr; border: none; }
    RoomsScreen #room-channel { height: 3; }
    RoomsScreen #room-main { width: 1fr; }
    RoomsScreen #room-heading { height: 2; padding: 0 1; text-style: bold; }
    RoomsScreen #room-transcript, RoomsScreen #room-board { height: 1fr; padding: 0 2; }
    RoomsScreen .room-message { height: auto; margin-bottom: 1; }
    RoomsScreen .task { height: auto; padding: 1 0; border-top: solid $primary-muted; }
    RoomsScreen .task-summary, RoomsScreen .room-note { height: auto; }
    RoomsScreen .task-actions { height: auto; layout: horizontal; }
    RoomsScreen .task-actions Button { min-width: 7; height: 3; margin-right: 0; }
    RoomsScreen #room-goal { height: 5; margin: 1 0; }
    RoomsScreen #room-board Input { margin-bottom: 1; }
    RoomsScreen #room-status { height: auto; max-height: 5; padding: 0 1; color: $warning; }
    RoomsScreen #room-compose-row { height: 3; }
    RoomsScreen #room-composer { width: 1fr; }
    RoomsScreen #room-suggestions { height: auto; max-height: 7; display: none; }
    RoomsScreen #room-hint { height: 2; padding: 0 1; color: $text-muted; }
    RoomsScreen.narrow #room-sidebar { width: 19; }
    RoomsScreen.narrow .task-actions { layout: vertical; }
    """

    def __init__(self, project: Path, viewer: str = "owner") -> None:
        """Retain a per-session draft identity independently from model identities."""
        super().__init__()
        self.project = project
        self.viewer = viewer
        self.runtime: RoomRuntime | None = None
        self.room = "general"
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

    def compose(self) -> ComposeResult:
        """Keep navigation, transcript and composer as separate focus/scroll regions."""
        with Horizontal(id="rooms-bar"):
            yield Button("Channels", id="rooms-toggle")
            yield Static("Project rooms", id="rooms-title")
            yield Button("Sessions", id="rooms-sessions")
        with Horizontal(id="rooms-layout"):
            with Vertical(id="room-sidebar"):
                yield OptionList(id="room-list")
                yield Input(placeholder="+ channel name", id="room-channel")
            with Vertical(id="room-main"):
                yield Static("#general", id="room-heading", markup=False)
                yield VerticalScroll(id="room-transcript")
                yield VerticalScroll(id="room-board")
                yield Static("", id="room-status", markup=False)
                yield OptionList(id="room-suggestions")
                with Horizontal(id="room-compose-row"):
                    yield Input(placeholder="Message #general…", id="room-composer")
                    yield Button("Send", id="room-send", variant="primary")
                yield Static(
                    "@agent replies read-only · Shared board → review scope → Start",
                    id="room-hint",
                    markup=False,
                )

    async def on_mount(self) -> None:
        """Start an idle foreground runtime, with no model calls."""
        self.query_one("#room-board").display = False
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
        self.set_class(event.size.width < NARROW_WIDTH, "narrow")
        self.query_one("#room-sidebar").display = event.size.width >= COLLAPSE_WIDTH

    def status(self, text: str) -> None:
        """Display actionable setup, approval and persistence failures."""
        self.query_one("#room-status", Static).update(text)

    async def switch(self, room: str) -> None:
        """Persist the old draft before reading the selected room."""
        if not self.runtime:
            return
        composer = self.query_one("#room-composer", Input)
        if self.loaded and not self.board:
            await self.runtime.db("view", self.viewer, self.room, composer.value)
        self.completions = []
        self.completion_value = ""
        self.query_one("#room-suggestions").display = False
        self.board = room == "__board"
        if not self.board:
            self.room = room
            view = await self.runtime.db("view", self.viewer, room, seen=True)
            self.updating = True
            composer.value = view["draft"]
            self.updating = False
            composer.placeholder = f"Message {room if room.startswith('dm:') else '#' + room}…"
            self.loaded = True
            self.last_message = 0
            await self.query_one("#room-transcript").remove_children()
        self.query_one("#room-transcript").display = not self.board
        self.query_one("#room-board").display = self.board
        self.query_one("#room-compose-row").display = not self.board
        self.query_one("#room-heading", Static).update(
            "Shared board"
            if self.board
            else (self.room[3:].title() if self.room.startswith("dm:") else "#" + self.room)
        )
        self.snapshot = ""
        await self.poll()
        if not self.board:
            composer.focus()

    async def poll(self) -> None:
        """Append messages without pulling a reader away from their scroll position."""
        if not self.runtime or self.refreshing or self.leaving:
            return
        self.refreshing = True
        try:
            if self.runtime.error != self.runtime_error:
                self.runtime_error = self.runtime.error
                self.status(self.runtime_error)
            rooms = await self.runtime.db("rows", "SELECT * FROM rooms ORDER BY rowid")
            options = []
            for room in rooms:
                view = await self.runtime.db("view", self.viewer, room["id"])
                count = await self.runtime.db(
                    "rows",
                    "SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>?",
                    (room["id"], view["seen"]),
                )
                suffix = f" ({count[0]['n']})" if count[0]["n"] else ""
                label = ("DM · " if room["id"].startswith("dm:") else "") + room["name"] + suffix
                options.append((room["id"], label))
            nav = json.dumps(options)
            if nav != self.navigation:
                self.navigation = nav
                listing = self.query_one("#room-list", OptionList)
                listing.clear_options()
                listing.add_options(
                    [Option(Text(label), id=key) for key, label in options]
                    + [Option("Shared board", id="__board")]
                )
            if self.board:
                await self.render_board()
            else:
                messages = await self.runtime.db("history", self.room)
                scroll = self.query_one("#room-transcript", VerticalScroll)
                follow = scroll.is_vertical_scroll_end
                for message in messages:
                    if message["id"] > self.last_message:
                        author = (
                            "You" if message["author"] == "owner" else message["author"].title()
                        )
                        await scroll.mount(
                            Static(Text(f"{author}\n{message['text']}"), classes="room-message")
                        )
                        self.last_message = message["id"]
                if follow:
                    scroll.scroll_end(animate=False)
                    await self.runtime.db("view", self.viewer, self.room, seen=True)
            jobs = await self.runtime.db(
                "rows",
                "SELECT * FROM jobs WHERE status IN ('working','queued','blocked','paused') "
                "ORDER BY rowid DESC LIMIT 5",
            )
            self.query_one("#room-hint", Static).update(
                " · ".join(f"{j['agent'].title()}: {j['status']}" for j in jobs)
                or "@agent replies read-only · Shared board → review scope → Start"
            )
        except Exception as exc:  # noqa: BLE001 - Poll errors must leave the composer usable.
            self.status(str(exc))
        finally:
            self.refreshing = False

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
            Label(f"New task in {self.room} · Goal"),
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

    @on(Input.Changed, "#room-composer")
    async def draft_changed(self, event: Input.Changed) -> None:
        """Persist the current draft and distinguish agent and file completions."""
        if (
            not self.runtime
            or self.board
            or self.updating
            or event.value != self.query_one("#room-composer", Input).value
        ):
            return
        runtime, room = self.runtime, self.room
        listing = self.query_one("#room-suggestions", OptionList)
        listing.display = False
        listing.clear_options()
        self.completions = []
        self.completion_value = ""
        await runtime.db("view", self.viewer, room, event.value)
        match = re.search(r"(?:^|\s)@([^\s]*)$", event.value)
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
            or event.value != self.query_one("#room-composer", Input).value
        ):
            return
        self.completions = [name for name, _ in suggestions]
        self.completion_value = event.value
        listing.add_options([Option(Text(label)) for _, label in suggestions])
        listing.display = bool(suggestions)

    @on(OptionList.OptionSelected, "#room-suggestions")
    def complete(self, event: OptionList.OptionSelected) -> None:
        """Insert the chosen mention without consuming file syntax."""
        composer = self.query_one("#room-composer", Input)
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
    async def submitted(self, event: Input.Submitted) -> None:
        """Handle composer submission and owner-created channels."""
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
        """Dispatch only an explicit nonempty owner message."""
        composer = self.query_one("#room-composer", Input)
        if composer.value.strip() and self.runtime:
            await self.runtime.send(self.room, composer.value)
            composer.value = ""
            await self.runtime.db("view", self.viewer, self.room, "")
            await self.poll()

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
            elif self.runtime:
                await self.owner_action(key)
        except Exception as exc:  # noqa: BLE001 - Owner operations surface actionable failures.
            self.status(str(exc))
        finally:
            if event.button.is_mounted:
                event.button.disabled = False

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

    def action_latest(self) -> None:
        """Jump to the latest message only on request."""
        self.query_one("#room-transcript", VerticalScroll).scroll_end(animate=False)

    async def action_sessions(self) -> None:
        """Return to existing session tabs; pause room execution before leaving."""
        self.leaving = True
        if self.runtime:
            if not self.board:
                await self.runtime.db(
                    "view", self.viewer, self.room, self.query_one("#room-composer", Input).value
                )
            await self.runtime.close()
            self.runtime = None
        self.app.pop_screen()

    async def on_unmount(self) -> None:
        """Stop room workers when the app closes or removes this screen."""
        if self.runtime:
            await self.runtime.close()
            self.runtime = None
