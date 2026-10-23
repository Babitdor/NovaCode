"""Independent Caveman feature switches; edits take effect only after Save."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Select, SelectionList, Static

from novacode_cli.config.nova_config import CAVEMAN_LEVELS

if TYPE_CHECKING:
    from textual.app import ComposeResult

FEATURES = {
    "say": "Say less — concise prose, exact code",
    "read": "Read less — compact logs / JSON / CSV, with exact recovery",
    "auto": "Auto — configured /router models and effort per request",
    "blocks": "Blocks — retain and reuse session scripts through normal tools",
    "waste": "Waste fixes — local token audit and cache observations",
    "browse": "Browse — compact HTML tool results, with exact recovery",
}


@dataclass(frozen=True)
class CavemanOptions:
    """A complete draft, persisted together only when Save is pressed."""

    features: dict[str, bool]
    level: str


class CavemanScreen(ModalScreen[CavemanOptions | None]):
    """Edit six independent preferences without changing the master switch."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    CavemanScreen #modal-box { width: 86; max-width: 96%; height: 75%; }
    CavemanScreen #caveman-features { height: 1fr; margin: 1 0; }
    CavemanScreen #modal-buttons { height: 3; }
    CavemanScreen Button { width: 1fr; min-width: 8; }
    """

    def __init__(
        self, features: dict[str, bool], *, router_ready: bool, level: str = "full"
    ) -> None:
        """Take a preference snapshot; persistence belongs to the app callback."""
        super().__init__()
        self._features = features
        self._router_ready = router_ready
        self._level = level if level in CAVEMAN_LEVELS else "full"

    def compose(self) -> ComposeResult:
        """Show persistent switches and the routing prerequisite."""
        with Vertical(id="modal-box"):
            yield Static("Caveman features", id="modal-title")
            yield Static(
                "Space toggles. Master switch below the composer activates selected features."
            )
            yield Select(
                [(f"Say less: {name}", name) for name in CAVEMAN_LEVELS],
                value=self._level,
                allow_blank=False,
                prompt="Say less intensity",
                id="caveman-level",
                tooltip="Say less intensity. Full matches the Caveman skill default.",
            )
            yield SelectionList[str](
                *((label, key, self._features.get(key, True)) for key, label in FEATURES.items()),
                id="caveman-features",
            )
            yield Static(
                "Auto: routes configured. Effort is set per route in /router."
                if self._router_ready
                else "Auto: configure model choices and criteria in /router first."
            )
            yield Static("Waste report: /caveman audit. No paid background cache-warming requests.")
            with Horizontal(id="modal-buttons"):
                yield Button("Save", id="caveman-save", variant="primary")
                yield Button("Cancel", id="caveman-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return a complete feature map, or discard the draft."""
        event.stop()
        if event.button.id == "caveman-save":
            selected = self.query_one(SelectionList).selected
            self.dismiss(
                CavemanOptions(
                    {key: key in selected for key in FEATURES},
                    str(self.query_one("#caveman-level", Select).value),
                )
            )
        elif event.button.id == "caveman-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        """Discard unsaved changes."""
        self.dismiss(None)
