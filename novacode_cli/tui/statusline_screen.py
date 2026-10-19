"""Keyboard-friendly status-line picker with a live preview."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, SelectionList, Static

from novacode_cli.tui.statusline import DEFAULT_FIELDS, FIELDS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from textual.app import ComposeResult


class StatusLineScreen(ModalScreen[list[str] | None]):
    """Preview preferences without applying them until Save is pressed."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    StatusLineScreen #modal-box { width: 80; max-width: 96%; height: 85%; }
    StatusLineScreen #statusline-preview {
        height: 3; padding: 1; background: $panel; color: $text; text-overflow: ellipsis;
    }
    StatusLineScreen #statusline-fields { height: 1fr; margin: 1 0; }
    StatusLineScreen #modal-buttons { height: 3; }
    StatusLineScreen Button { min-width: 8; width: 1fr; margin: 0 1 0 0; }
    """

    def __init__(self, selected: Sequence[str], preview: Callable[[Sequence[str]], Text]) -> None:
        """Take a snapshot of the settings and a side-effect-free renderer."""
        super().__init__()
        self._selected = set(selected)
        self._preview = preview

    def compose(self) -> ComposeResult:
        """Show the preview, scrollable checklist, and explicit save controls."""
        with Vertical(id="modal-box"):
            yield Static("Status line", id="modal-title")
            yield Static("Choose what appears above the composer. Space toggles; Escape cancels.")
            yield Static(id="statusline-preview")
            yield SelectionList[str](
                *((label, key, key in self._selected) for key, label in FIELDS.items()),
                id="statusline-fields",
            )
            with Horizontal(id="modal-buttons"):
                yield Button("Save", id="statusline-save", variant="primary")
                yield Button("Reset", id="statusline-reset")
                yield Button("Cancel", id="statusline-cancel")

    def on_mount(self) -> None:
        """Paint the initial preview and start keyboard navigation on the list."""
        self._update_preview()
        self.query_one(SelectionList).focus()

    def _update_preview(self) -> None:
        fields = self.query_one(SelectionList).selected
        preview = self._preview(fields)
        self.query_one("#statusline-preview", Static).update(
            preview or Text("Status line hidden", style="dim")
        )
        self.query_one("#modal-title", Static).update(
            "Status line · unsaved changes" if set(fields) != self._selected else "Status line"
        )

    def on_selection_list_selected_changed(self) -> None:
        """Update the preview on every checkbox change."""
        self._update_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Save, restore defaults in the draft, or discard the draft."""
        if event.button.id == "statusline-save":
            selected = set(self.query_one(SelectionList).selected)
            self.dismiss([key for key in FIELDS if key in selected])
        elif event.button.id == "statusline-reset":
            choices = self.query_one(SelectionList)
            with choices.prevent(SelectionList.SelectedChanged):
                choices.deselect_all()
                for key in DEFAULT_FIELDS:
                    choices.select(key)
            self._update_preview()
        elif event.button.id == "statusline-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        """Discard pending changes."""
        self.dismiss(None)
