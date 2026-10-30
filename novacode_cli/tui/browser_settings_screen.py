"""Explicit browser-server selection, saved only after the owner presses Save."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, SelectionList, Static

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from textual.app import ComposeResult


class BrowserSettingsScreen(ModalScreen[list[str] | None]):
    """Select exact configured server names without enabling or installing anything."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    BrowserSettingsScreen #modal-box { width: 80; max-width: 96%; height: 85%; }
    BrowserSettingsScreen #browser-servers { height: 1fr; margin: 1 0; }
    BrowserSettingsScreen #modal-buttons { height: 3; }
    BrowserSettingsScreen Button { min-width: 8; width: 1fr; margin: 0 1 0 0; }
    """

    def __init__(self, servers: Mapping[str, bool], selected: Sequence[str]) -> None:
        """Snapshot configured/disabled state and preserve missing selected servers."""
        super().__init__()
        self._servers = dict(servers)
        self._selected = set(selected)
        self._names = sorted(set(servers) | self._selected)

    def compose(self) -> ComposeResult:
        """Show an explicit trust checklist and keep Save/Cancel reachable on small terminals."""
        with Vertical(id="modal-box"):
            yield Static("Browser Mode · trusted MCP servers", id="modal-title")
            yield Static(
                "Select browser servers you trust. Only reviewed browser actions are allowed. "
                "Shell, files, driver-code execution and generic dispatch stay blocked. "
                "No selection keeps web search/fetch only. Disabled servers stay disabled."
            )
            yield SelectionList[str](
                *((self._label(name), name, name in self._selected) for name in self._names),
                id="browser-servers",
            )
            with Horizontal(id="modal-buttons"):
                yield Button("Save", id="browser-settings-save", variant="primary")
                yield Button("Cancel", id="browser-settings-cancel")

    def _label(self, name: str) -> str:
        if name not in self._servers:
            return f"{name} (not configured)"
        return f"{name} (disabled)" if self._servers[name] else name

    def on_mount(self) -> None:
        """Start keyboard navigation in the checklist."""
        self.query_one(SelectionList).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return only the draft; the caller owns persistence and busy-session checks."""
        if event.button.id == "browser-settings-save":
            self.dismiss(list(self.query_one(SelectionList).selected))
        elif event.button.id == "browser-settings-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        """Discard pending selections."""
        self.dismiss(None)
