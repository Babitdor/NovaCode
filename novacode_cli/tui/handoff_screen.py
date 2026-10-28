"""Named project handoffs with literal, scrollable previews and explicit continuation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from novacode_cli.handoff import HandoffMemory, Record, document

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType


class HandoffScreen(ModalScreen[str | None]):
    """Browsing never runs an agent; only the labelled Continue button returns a selection."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    DEFAULT_CSS = """
    HandoffScreen { align: center middle; }
    HandoffScreen #handoff-shell {
        width: 94%; max-width: 120; height: 92%; padding: 1 2;
        border: round $accent; background: $surface;
    }
    HandoffScreen #handoff-title { height: 1; color: $primary; text-style: bold; }
    HandoffScreen #handoff-help { height: auto; max-height: 3; color: $text-muted; }
    HandoffScreen #handoff-search { height: 3; }
    HandoffScreen #handoff-body { height: 1fr; min-height: 4; }
    HandoffScreen #handoff-list { width: 36%; height: 1fr; border: none; }
    HandoffScreen #handoff-preview-scroll { width: 1fr; height: 1fr; padding: 0 1; }
    HandoffScreen #handoff-preview { height: auto; }
    HandoffScreen #handoff-actions { height: auto; align-horizontal: right; }
    HandoffScreen #handoff-actions Button { height: 3; min-width: 10; }
    HandoffScreen.compact #handoff-shell { width: 100%; height: 100%; padding: 0 1; }
    HandoffScreen.compact #handoff-help { display: none; }
    HandoffScreen.compact #handoff-body { layout: vertical; }
    HandoffScreen.compact #handoff-list { width: 100%; height: 4; }
    HandoffScreen.compact #handoff-preview-scroll { width: 100%; height: 1fr; }
    """

    def __init__(self, directory: str, *, can_continue: bool = True, identity: str = "") -> None:
        """Preview this project's records, optionally highlighting an explicit selection."""
        super().__init__()
        self.directory = directory
        self.can_continue = can_continue
        self.identity = identity
        self.records: list[Record] = []
        self.visible_records: list[Record] = []

    def compose(self) -> ComposeResult:
        """Keep the list, preview and continuation controls inside one modal."""
        with Vertical(id="handoff-shell"):
            yield Static("PROJECT HANDOFFS", id="handoff-title")
            yield Static(
                "Select a handoff to preview. Continue saves this chat and starts a fresh one "
                "in the current workspace, with the current model and permissions.",
                id="handoff-help",
            )
            yield Input(placeholder="Search this project's handoffs…", id="handoff-search")
            with Horizontal(id="handoff-body"):
                yield OptionList(id="handoff-list")
                with VerticalScroll(id="handoff-preview-scroll", can_focus=True):
                    yield Static("Loading handoffs…", id="handoff-preview", markup=False)
            with Horizontal(id="handoff-actions"):
                yield Button("Continue in new session", id="handoff-continue", disabled=True)
                yield Button("Close", id="handoff-close")

    def on_resize(self) -> None:
        """Stack the preview beneath the list on small terminals."""
        self.set_class(self.size.width < 75 or self.size.height < 28, "compact")  # noqa: PLR2004 - Layout breakpoints.

    def on_mount(self) -> None:
        """Load records without invoking any model or execution tool."""
        self.load_records()

    @work(exclusive=True)
    async def load_records(self) -> None:
        """Read saved project records off the UI thread."""
        try:
            self.records = await asyncio.to_thread(lambda: HandoffMemory(self.directory).records())
        except Exception:  # noqa: BLE001 - Errors belong in temporary notifications.
            self.notify("Handoff storage unavailable.", severity="warning")
            self.query_one("#handoff-preview", Static).update("Handoffs could not be loaded.")
            return
        self.filter_records()
        self.query_one("#handoff-list").focus()

    @on(Input.Changed, "#handoff-search")
    def filter_records(self) -> None:
        """Search by task name; users never need to type record IDs."""
        query = self.query_one("#handoff-search", Input).value.strip().casefold()
        self.visible_records = [
            record for record in self.records if query in str(record["title"]).casefold()
        ]
        options = self.query_one("#handoff-list", OptionList)
        options.clear_options()
        for record in self.visible_records:
            when = datetime.fromtimestamp(record["updated_at"], UTC).strftime("%d %b %H:%M")
            options.add_option(Option(Text(f"{record['title']}\n{when} · {record['status']}")))
        options.highlighted = next(
            (i for i, record in enumerate(self.visible_records) if record["id"] == self.identity),
            0 if self.visible_records else None,
        )
        self.preview()

    def selected(self) -> Record | None:
        """Resolve the highlighted record in the filtered list."""
        index = self.query_one("#handoff-list", OptionList).highlighted
        return (
            self.visible_records[index]
            if index is not None and index < len(self.visible_records)
            else None
        )

    @on(OptionList.OptionHighlighted, "#handoff-list")
    def preview(self) -> None:
        """Show literal context and leave execution behind an explicit button."""
        record = self.selected()
        self.query_one("#handoff-preview", Static).update(
            document(record)
            if record
            else "No saved handoffs match. Enable Handoff, then send a task."
        )
        self.query_one("#handoff-continue", Button).disabled = not (record and self.can_continue)
        self.query_one("#handoff-preview-scroll", VerticalScroll).scroll_home(animate=False)

    @on(OptionList.OptionSelected, "#handoff-list")
    def focus_preview(self) -> None:
        """Enter on a task opens the keyboard-scrollable preview."""
        self.query_one("#handoff-preview-scroll").focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        """Return an identity only after an explicit continuation action."""
        event.stop()
        if (
            event.button.id == "handoff-continue"
            and self.can_continue
            and (record := self.selected())
        ):
            self.dismiss(record["id"])
        elif event.button.id == "handoff-close":
            self.action_close()

    def action_close(self) -> None:
        """Dismiss without changing the selected session or running work."""
        self.dismiss(None)
