"""``/mcp`` → Tools: choose which of a server's tools Nova may use."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, SelectionList, Static

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.app import ComposeResult
    from textual.binding import BindingType


def _size(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)  # noqa: PLR2004


class McpToolsScreen(ModalScreen[list[str] | None]):
    """A checklist of one server's tools; dismisses with the chosen names or None."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    McpToolsScreen #modal-box { width: 100; max-width: 96%; height: 90%; }
    McpToolsScreen #tools-summary { color: $text-muted; }
    McpToolsScreen #tools-filter { margin: 1 0 0 0; }
    McpToolsScreen #tools-list { height: 1fr; margin: 1 0; }
    McpToolsScreen #modal-buttons { height: 3; }
    McpToolsScreen #modal-buttons Button { min-width: 8; width: 1fr; margin: 0 1 0 0; }
    """

    def __init__(
        self,
        server: str,
        tools: Sequence[tuple[str, str, int]],
        allowed: Sequence[str] | None,
    ) -> None:
        """``tools``: (name, description, schema chars); ``allowed``: the current allowlist."""
        super().__init__()
        self._server = server
        self._tools = sorted(tools)
        self._sizes = {name: chars for name, _, chars in self._tools}
        self._checked = {
            name
            for name, _, _ in self._tools
            if allowed is None or any(fnmatchcase(name, p) for p in allowed)
        }

    def compose(self) -> ComposeResult:
        """Summary, filter, checklist and the save controls."""
        with Vertical(id="modal-box"):
            yield Static(Text(f"Tools · {self._server}", style="bold"), id="modal-title")
            yield Static("", id="tools-summary")
            yield Input(placeholder="Filter by name or description", id="tools-filter")
            yield SelectionList[str](id="tools-list")
            with Horizontal(id="modal-buttons"):
                yield Button("Save", id="tools-save", variant="primary")
                yield Button("All", id="tools-all")
                yield Button("None", id="tools-none")
                yield Button("Cancel", id="tools-cancel")

    def on_mount(self) -> None:
        """Fill the list and start on it, so Space toggles straight away."""
        self._refill()
        self.query_one("#tools-list", SelectionList).focus()

    def _visible(self) -> list[tuple[str, str, int]]:
        needle = self.query_one("#tools-filter", Input).value.strip().casefold()
        return [t for t in self._tools if not needle or needle in f"{t[0]} {t[1]}".casefold()]

    def _refill(self) -> None:
        """Rebuild the visible options; checks live in ``_checked`` across filters."""
        choices = self.query_one("#tools-list", SelectionList)
        with choices.prevent(SelectionList.SelectedChanged):
            choices.clear_options()
            for name, description, chars in self._visible():
                label = Text(name)
                label.append(f"  {_size(chars)}", style="yellow" if chars > 10_000 else "dim")  # noqa: PLR2004
                if description:
                    label.append(f"  {' '.join(description.split())[:70]}", style="dim")
                choices.add_option((label, name, name in self._checked))
        self._update_summary()

    def _update_summary(self) -> None:
        chars = sum(self._sizes[n] for n in self._checked)
        total = sum(self._sizes.values())
        self.query_one("#tools-summary", Static).update(
            f"{len(self._checked)} of {len(self._tools)} tools selected · "
            f"{_size(chars)} of {_size(total)} chars of schemas. Fewer, smaller tools "
            "keep requests small. Space toggles; Escape cancels."
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Narrow the list as the filter is typed."""
        if event.input.id == "tools-filter":
            self._refill()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        """Enter in the filter moves to the list."""
        self.query_one("#tools-list", SelectionList).focus()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        """Record checks for the visible tools only; hidden ones keep their state."""
        selected = set(event.selection_list.selected)
        for name, _, _ in self._visible():
            if name in selected:
                self._checked.add(name)
            else:
                self._checked.discard(name)
        self._update_summary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Save, select all/none of the visible tools, or cancel."""
        button = event.button.id
        if button == "tools-save":
            self.dismiss(sorted(self._checked))
        elif button in {"tools-all", "tools-none"}:
            for name, _, _ in self._visible():
                (self._checked.add if button == "tools-all" else self._checked.discard)(name)
            self._refill()
        elif button == "tools-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        """Discard the changes."""
        self.dismiss(None)
