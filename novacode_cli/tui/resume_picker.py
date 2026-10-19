"""Searchable, theme-aware resume dialogs shared by startup and chat."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from novacode_cli.tui.screens import PickScreen

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual import events
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from novacode_cli.session.session_persistence import SessionMeta

_DETAILS_MIN_WIDTH = 96
_SHORT_HEIGHT = 27
_TINY_HEIGHT = 18


class ResumeSessionScreen(PickScreen):
    """Browse session metadata without loading conversations or changing sessions."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+f", "search", "Search"),
        ("down", "focus_sessions", "Sessions"),
    ]
    DEFAULT_CSS = """
    ResumeSessionScreen { align: center middle; background: $background 85%; }
    ResumeSessionScreen #resume-shell {
        width: 94%; max-width: 132; height: 88%; max-height: 44;
        border: round $border 70%; background: $background; padding: 1 2;
    }
    ResumeSessionScreen #modal-title { height: 1; margin: 0; color: $accent; text-style: bold; }
    ResumeSessionScreen #resume-subtitle { height: 2; color: $text-muted; }
    ResumeSessionScreen #resume-search {
        height: 3; margin: 0; border: round $border 50%; background: $surface;
    }
    ResumeSessionScreen #resume-search:focus { border: round $accent; }
    ResumeSessionScreen #resume-count { height: 2; padding-top: 1; color: $text-muted; }
    ResumeSessionScreen #resume-main { height: 1fr; min-height: 1; }
    ResumeSessionScreen #resume-list-column { width: 3fr; height: 1fr; }
    ResumeSessionScreen #pick-list {
        height: 1fr; max-height: 100%; margin: 0; padding: 0;
        border: none; background: transparent;
    }
    ResumeSessionScreen #pick-list > .option-list--option { padding: 0 1; color: $text; }
    ResumeSessionScreen #pick-list > .option-list--option-highlighted {
        background: $accent 16%; color: $text; text-style: none;
    }
    ResumeSessionScreen #pick-list:focus > .option-list--option-highlighted {
        background: $accent 24%; color: $text;
    }
    ResumeSessionScreen #resume-details {
        width: 2fr; height: 1fr; margin-left: 2; padding: 1 2;
        border-left: solid $border 40%; background: $surface;
    }
    ResumeSessionScreen .resume-label { height: 1; color: $accent; text-style: bold; }
    ResumeSessionScreen .resume-value { height: auto; margin-bottom: 1; color: $text; }
    ResumeSessionScreen #resume-status.resume-blocked { color: $warning; }
    ResumeSessionScreen #resume-status.resume-complete { color: $success; }
    ResumeSessionScreen #resume-empty { height: auto; padding: 2; color: $text-muted; }
    ResumeSessionScreen #modal-buttons { height: 3; margin: 1 0 0 0; align: right middle; }
    ResumeSessionScreen #modal-buttons Button { min-width: 10; margin: 0 0 0 1; }
    ResumeSessionScreen #pick-hint { height: 1; color: $text-muted; }
    ResumeSessionScreen.resume-narrow #resume-details { display: none; }
    ResumeSessionScreen.resume-no-results #resume-details { display: none; }
    ResumeSessionScreen.resume-short #resume-shell { height: 95%; padding: 0 1; }
    ResumeSessionScreen.resume-short #resume-subtitle { display: none; }
    ResumeSessionScreen.resume-short #resume-search { height: 1; border: none; }
    ResumeSessionScreen.resume-short #resume-count { height: 1; padding: 0; }
    ResumeSessionScreen.resume-short #modal-buttons { margin: 0; }
    ResumeSessionScreen.resume-short #resume-details { display: none; }
    ResumeSessionScreen.resume-tiny #pick-hint { display: none; }
    ResumeSessionScreen.classic-ui #resume-shell,
    ResumeSessionScreen.classic-ui #resume-search { border: solid $primary 50%; }
    ResumeSessionScreen.minimal-ui #resume-shell { border: none; background: $surface; }
    ResumeSessionScreen.minimal-ui #resume-details { background: transparent; }
    ResumeSessionScreen.centered-ui #resume-shell { background: $panel; }
    ResumeSessionScreen.resume-short #resume-search { border: none; }
    ResumeSessionScreen.resume-short #pick-list {
        height: 1fr; max-height: 100%; border: none; padding: 0;
    }
    """

    def __init__(self, sessions: Sequence[SessionMeta], *, title: str = "Resume a session") -> None:
        """Keep the original session order and return its index after filtering."""
        super().__init__(title, [])
        self.add_class("resume-dialog")
        self._sessions = list(sessions)
        self._filtered: list[int] = []

    def compose(self) -> ComposeResult:
        """Build a compact browser with a separate, scrollable details pane."""
        with Vertical(id="resume-shell"):
            yield Static(self._title, id="modal-title", markup=False)
            yield Static("Choose a saved conversation to continue.", id="resume-subtitle")
            yield Input(
                placeholder="Search task, project, model or session ID…", id="resume-search"
            )
            yield Static("", id="resume-count")
            with Horizontal(id="resume-main"):
                with Vertical(id="resume-list-column"):
                    yield OptionList(id="pick-list")
                    yield Static("", id="resume-empty", markup=False)
                with VerticalScroll(id="resume-details"):
                    for label, key in (
                        ("SESSION", "identity"),
                        ("PROJECT", "project"),
                        ("MODEL", "model"),
                        ("TASK", "task"),
                        ("STATUS", "status"),
                        ("NEXT STEP", "next"),
                    ):
                        yield Static(label, classes="resume-label")
                        yield Static("", id=f"resume-{key}", classes="resume-value", markup=False)
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="select", variant="primary")
            yield Static("↑/↓ select   Enter continue   Ctrl+F search   Esc cancel", id="pick-hint")

    def on_mount(self) -> None:
        """Populate metadata and start with a keyboard-ready selection."""
        self._filter("")
        self._resize_layout(self.size.width, self.size.height)
        self.query_one("#pick-list", OptionList).focus()

    def on_resize(self, event: events.Resize) -> None:
        """Keep search, selection and actions usable in compact terminals."""
        self._resize_layout(event.size.width, event.size.height)
        if self.is_mounted:
            self.call_after_refresh(self.query_one("#pick-list", OptionList).scroll_to_highlight)

    def _resize_layout(self, width: int, height: int) -> None:
        self.set_class(width < _DETAILS_MIN_WIDTH, "resume-narrow")
        self.set_class(height < _SHORT_HEIGHT, "resume-short")
        self.set_class(height < _TINY_HEIGHT, "resume-tiny")

    def _filter(self, query: str) -> None:
        from novacode_cli.session.session_restore import format_session_age

        listing = self.query_one("#pick-list", OptionList)
        previous = self._selected_index()
        terms = query.casefold().split()
        self._filtered = []
        listing.clear_options()
        for index, meta in enumerate(self._sessions):
            haystack = " ".join(
                str(value or "")
                for value in (
                    meta.session_id,
                    meta.current_task,
                    meta.project_root,
                    meta.model_name,
                    getattr(meta, "model_provider", None),
                )
            ).casefold()
            if not all(term in haystack for term in terms):
                continue
            self._filtered.append(index)
            project = Path(meta.project_root).name if meta.project_root else "Untitled"
            title = " ".join((meta.current_task or f"{project} conversation").split())
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(title, style="bold")
            row.append(
                f"\n{meta.session_id[:8]}  ·  {meta.message_count} messages  ·  "
                f"{format_session_age(meta.last_active)}",
                style="dim",
            )
            row.append("\n ")
            listing.add_option(Option(row))
        count = len(self._filtered)
        self.set_class(not count, "resume-no-results")
        self.query_one("#resume-count", Static).update(
            f"{count} of {len(self._sessions)} saved sessions"
        )
        listing.display = bool(count)
        empty = self.query_one("#resume-empty", Static)
        empty.display = not count
        empty.update(
            "No matching sessions.\nTry another task, project, model or ID."
            if self._sessions
            else "No saved sessions yet.\nYour conversations will appear here after saving."
        )
        self.query_one("#select", Button).disabled = not count
        listing.highlighted = (
            (
                self._filtered.index(previous)
                if previous is not None and previous in self._filtered
                else 0
            )
            if count
            else None
        )
        self._update_details()

    def _selected_index(self) -> int | None:
        index = self.query_one("#pick-list", OptionList).highlighted
        return (
            self._filtered[index]
            if index is not None and 0 <= index < len(self._filtered)
            else None
        )

    def _update_details(self) -> None:
        from novacode_cli.session.session_restore import format_session_age

        index = self._selected_index()
        values: dict[str, str] = {}
        if index is not None:
            meta = self._sessions[index]
            model = meta.model_name or "Unknown model"
            provider = getattr(meta, "model_provider", None)
            model = (
                f"{provider} / {model}"
                if provider
                else f"{model}\nModel not restored (legacy session)"
            )
            values = {
                "identity": (
                    f"{meta.session_id}\n{format_session_age(meta.last_active)}"
                    f" · {meta.message_count} messages"
                ),
                "project": meta.project_root or "No project recorded",
                "model": model,
                "task": meta.current_task or "No task recorded",
                "status": (meta.task_status or "active").capitalize()
                + (f"\n{meta.blocked_reason}" if getattr(meta, "blocked_reason", None) else ""),
                "next": (
                    getattr(meta, "next_step_hint", None)
                    or getattr(meta, "blocked_reason", None)
                    or "No next step recorded"
                ),
            }
        for key in ("identity", "project", "model", "task", "status", "next"):
            self.query_one(f"#resume-{key}", Static).update(values.get(key, "—"))
        status = self.query_one("#resume-status", Static)
        state = self._sessions[index].task_status if index is not None else None
        status.set_class(state == "blocked", "resume-blocked")
        status.set_class(state == "complete", "resume-complete")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Search only the already loaded metadata."""
        if event.input.id == "resume-search":
            self._filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Continue the highlighted match from the search field."""
        if event.input.id == "resume-search":
            self._accept()

    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        """Show full, untruncated facts for the highlighted conversation."""
        self._update_details()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Return the original session index, rather than a filtered row index."""
        event.stop()
        event.prevent_default()
        self._accept()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Continue or cancel without changing session persistence."""
        event.stop()
        event.prevent_default()
        if event.button.id == "select":
            self._accept()
        elif event.button.id == "cancel":
            self.dismiss(-1)

    def _accept(self) -> None:
        index = self._selected_index()
        if index is not None:
            self.dismiss(index)

    def action_search(self) -> None:
        """Move directly to the search field."""
        self.query_one("#resume-search", Input).focus()

    def action_focus_sessions(self) -> None:
        """Let Down leave the search field without losing its query."""
        self.query_one("#pick-list", OptionList).focus()


class ResumeModeScreen(PickScreen):
    """Explain the two existing resume modes without changing their semantics."""

    DEFAULT_CSS = (
        ResumeSessionScreen.DEFAULT_CSS.replace("ResumeSessionScreen", "ResumeModeScreen")
        + """
    ResumeModeScreen #resume-shell { height: auto; max-height: 95%; max-width: 86; }
    ResumeModeScreen #pick-list { height: auto; max-height: 50vh; }
    """
    )

    def __init__(self) -> None:
        """Keep summary first and saved conversation second, as before."""
        super().__init__("How would you like to continue?", [])
        self.add_class("resume-dialog")

    def compose(self) -> ComposeResult:
        """Present both choices with their actual effect on conversation context."""
        with Vertical(id="resume-shell"):
            yield Static(self._title, id="modal-title")
            yield Static("Choose how Nova prepares the saved context.", id="resume-subtitle")
            yield OptionList(id="pick-list")
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Resume", id="select", variant="primary")
            yield Static("↑/↓ select   Enter resume   Esc cancel", id="pick-hint")

    def on_mount(self) -> None:
        """Use literal text so session content never becomes terminal markup."""
        listing = self.query_one("#pick-list", OptionList)
        for title, explanation in (
            ("Resume from a summary", "Summarize the conversation first, then continue."),
            ("Resume as saved", "Continue with the newest messages, without a new summary."),
        ):
            row = Text(title + "\n", style="bold")
            row.append(explanation, style="not bold dim")
            row.append("\n ")
            listing.add_option(Option(row))
        listing.highlighted = 0
        listing.focus()
        self._resize_layout(self.size.height)

    def on_resize(self, event: events.Resize) -> None:
        """Keep both choices and the footer reachable on short terminals."""
        self._resize_layout(event.size.height)

    def _resize_layout(self, height: int) -> None:
        self.set_class(height < _SHORT_HEIGHT, "resume-short")
        self.set_class(height < _TINY_HEIGHT, "resume-tiny")


# App-level rules must win over the shared picker and compact-dialog defaults.
RESUME_CSS = ResumeSessionScreen.DEFAULT_CSS + ResumeModeScreen.DEFAULT_CSS
