"""Exclusive compression mode and feature options, applied only after Save."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, SelectionList, Static

from novacode_cli.config.nova_config import CAVEMAN_LEVELS

if TYPE_CHECKING:
    from textual.app import ComposeResult

FEATURES = {
    "say": "Shorter replies (keep code exact)",
    "read": "Compact long tool output (originals recoverable)",
    "browse": "Compact HTML results (originals recoverable)",
    "waste": "Avoid repeating identical tool results",
    "blocks": "Remember reusable scripts in this session",
    "auto": "Use configured model routes (/router)",
}
DESCRIPTIONS = {
    "off": "Normal replies and tool output. Choose a style to see its settings.",
    "caveman": (
        "Shorter explanations, with optional reductions for long tool results. Code stays exact."
    ),
    "ponytail": (
        "Lean coding rules: favor smaller, focused changes and concise explanations. "
        "Tool results stay unchanged."
    ),
}
STRENGTHS = [("Gentle", "lite"), ("Standard", "full"), ("Strong", "ultra")]


@dataclass(frozen=True)
class CavemanOptions:
    """A complete draft, persisted together only when Save is pressed."""

    features: dict[str, bool]
    level: str
    mode: str = "off"
    ponytail_level: str = "full"


class CavemanScreen(ModalScreen[CavemanOptions | None]):
    """Edit Lite Mode as a draft, showing only the selected style's options."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    CavemanScreen { align: center middle; }
    CavemanScreen #modal-box { width: 82; max-width: 96%; height: 90%; padding: 1; }
    CavemanScreen #modal-title { height: 1; }
    CavemanScreen #compression-body { height: 1fr; }
    CavemanScreen .compression-label { height: auto; margin-top: 1; text-style: bold; }
    CavemanScreen .compression-help { height: auto; color: $text-muted; }
    CavemanScreen #caveman-section, CavemanScreen #ponytail-section { height: auto; }
    CavemanScreen #caveman-features { height: 8; margin: 1 0; }
    CavemanScreen #modal-buttons { height: 3; margin-top: 1; }
    CavemanScreen Button { width: 1fr; min-width: 8; }
    """

    def __init__(
        self,
        features: dict[str, bool],
        *,
        router_ready: bool,
        level: str = "full",
        mode: str = "off",
        ponytail_level: str = "full",
    ) -> None:
        """Take a preference snapshot; persistence belongs to the app callback."""
        super().__init__()
        self._mode = mode
        self._ponytail_level = ponytail_level
        self._features = dict(features)
        self._router_ready = router_ready
        self._level = level if level in CAVEMAN_LEVELS else "full"

    def compose(self) -> ComposeResult:
        """Keep Apply and Cancel fixed while the relevant settings scroll."""
        with Vertical(id="modal-box"):
            yield Static("Lite Mode · Compression", id="modal-title")
            with VerticalScroll(id="compression-body"):
                yield Static("Compression style", classes="compression-label")
                yield Select(
                    [
                        ("Off — disabled", "off"),
                        ("Caveman — concise replies", "caveman"),
                        ("Ponytail — lean coding", "ponytail"),
                    ],
                    value=self._mode,
                    allow_blank=False,
                    id="compression-mode",
                )
                yield Static("", id="compression-description", classes="compression-help")
                with Vertical(id="caveman-section"):
                    yield Static("Reply strength", classes="compression-label")
                    yield Select(
                        [
                            *STRENGTHS,
                            *(
                                (label + " · classical Chinese", "wenyan-" + value)
                                for label, value in STRENGTHS
                            ),
                        ],
                        value=self._level,
                        allow_blank=False,
                        id="caveman-level",
                    )
                    yield Static(
                        "Gentle keeps full sentences. Standard uses terse phrases. "
                        "Strong is very brief. Classical styles apply only to Chinese replies. "
                        "Reply strength applies when Shorter replies is checked.",
                        classes="compression-help",
                    )
                    yield Static("Caveman options · Space to toggle", classes="compression-label")
                    yield SelectionList[str](
                        *(
                            (label, key, self._features.get(key, True))
                            for key, label in FEATURES.items()
                        ),
                        id="caveman-features",
                    )
                    yield Static(
                        "Model routing uses the choices and effort configured in /router."
                        if self._router_ready
                        else "Model routing stays inactive until /router has routes.",
                        classes="compression-help",
                    )
                with Vertical(id="ponytail-section"):
                    yield Static("Coding strength", classes="compression-label")
                    yield Select(
                        STRENGTHS,
                        value=self._ponytail_level,
                        allow_blank=False,
                        id="ponytail-level",
                    )
                    yield Static(
                        "Gentle prefers the smaller solution. Standard applies lean coding rules. "
                        "Strong also challenges unnecessary scope.",
                        classes="compression-help",
                    )
                yield Static(
                    "Changes apply only after Apply. Inspect usage with /lite-mode audit.",
                    classes="compression-help",
                )
            with Horizontal(id="modal-buttons"):
                yield Button("Apply", id="caveman-save", variant="primary")
                yield Button("Cancel", id="caveman-cancel")

    def on_mount(self) -> None:
        """Set visibility before the user starts editing."""
        self.update_style()

    @on(Select.Changed, "#compression-mode")
    def update_style(self) -> None:
        """Preserve each style's draft while showing only its relevant controls."""
        mode = str(self.query_one("#compression-mode", Select).value)
        self.query_one("#caveman-section").display = mode == "caveman"
        self.query_one("#ponytail-section").display = mode == "ponytail"
        self.query_one("#compression-description", Static).update(DESCRIPTIONS.get(mode, ""))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return a complete feature map, or discard the draft."""
        event.stop()
        if event.button.id == "caveman-save":
            selected = self.query_one(SelectionList).selected
            self.dismiss(
                CavemanOptions(
                    {key: key in selected for key in FEATURES},
                    str(self.query_one("#caveman-level", Select).value),
                    str(self.query_one("#compression-mode", Select).value),
                    str(self.query_one("#ponytail-level", Select).value),
                )
            )
        elif event.button.id == "caveman-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        """Discard unsaved changes."""
        self.dismiss(None)
