"""The independent System One configuration panel inside `/model`."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Select, Static, Switch

from novacode_cli.config.nova_config import NovaConfig

if TYPE_CHECKING:
    from textual.app import ComposeResult


class DecisionSettings(VerticalScroll):
    """Stage decision settings until Save; Cancel has no side effects."""

    DEFAULT_CSS = """
    DecisionSettings { height: auto; max-height: 14; }
    DecisionSettings Horizontal { height: auto; align-vertical: middle; }
    DecisionSettings Static { height: auto; }
    DecisionSettings Input, DecisionSettings Select { margin-bottom: 1; }
    DecisionSettings #decision-status { margin-top: 1; }
    """

    class Saved(Message):
        """Settings were persisted and the agent can be rebuilt."""

        def __init__(self, *, enabled: bool, model: str) -> None:
            """Carry the persisted toggle and model to the picker."""
            super().__init__()
            self.enabled = enabled
            self.model = model

    def __init__(self, *, id: str) -> None:  # noqa: A002 — Textual's widget API
        """Create a hidden panel that loads when its tab is selected."""
        super().__init__(id=id)
        self.display = False
        self._loaded = False
        self._loading = False
        self._applying = False
        self._preset = "tev1"

    def compose(self) -> ComposeResult:
        """Show the toggle, presets and editable model/endpoint."""
        yield Static(
            "Decide which tool outputs to offload. Summary generation uses your chat model."
        )
        with Horizontal():
            yield Switch(value=False, id="decision-enabled", disabled=True)
            yield Static("Enable System One tool pruning")
        yield Select(
            [
                ("Jev · TypeSafe API", "jev"),
                ("Tev1 · local Ollama", "tev1"),
                ("Custom System One", "custom"),
            ],
            value="tev1",
            allow_blank=False,
            id="decision-preset",
            disabled=True,
        )
        yield Static("Decision model")
        yield Input(id="decision-model", placeholder="jev-latest, tev1:4b, kev1, …", disabled=True)
        yield Static("System One endpoint")
        yield Input(id="decision-endpoint", placeholder="https://…/v1/systemone", disabled=True)
        yield Button("Manage API key", id="decision-auth")
        yield Static("Loading decision settings…", id="decision-status")

    @work(thread=True, group="decision-load", exclusive=True)
    def load(self) -> None:
        """Load only when this tab opens, away from the event loop."""
        if self._loaded or self._loading:
            return
        self._loading = True
        try:
            config = NovaConfig()
            settings = (
                config.get_tool_verdicts_enabled(),
                config.get_tool_verdict_endpoint(),
                config.get_tool_verdict_model(),
            )
            self.app.call_from_thread(self._apply, *settings)
        except Exception:  # noqa: BLE001 — failed reads leave Save unavailable
            self.app.call_from_thread(
                self._hint, "Could not load decision settings. Reopen /model.", error=True
            )
        finally:
            self._loading = False

    def _apply(self, enabled: bool, endpoint: str, model: str) -> None:  # noqa: FBT001
        """Populate controls without treating restored presets as new picks."""
        self._applying = True
        self.query_one("#decision-enabled", Switch).value = enabled
        self.query_one("#decision-model", Input).value = model
        self.query_one("#decision-endpoint", Input).value = endpoint
        preset = "custom"
        if endpoint == NovaConfig.TOOL_VERDICT_JEV_ENDPOINT:
            preset = "jev"
        elif endpoint == NovaConfig.TOOL_VERDICT_DEFAULT_ENDPOINT and model.startswith("tev1"):
            preset = "tev1"
        self._preset = preset
        self.query_one("#decision-preset", Select).value = preset
        for widget in self.query("Switch, Input, Select"):
            widget.disabled = False
        self._loaded = True
        self._applying = False
        self._hint(
            "Save applies these settings to this session. API keys are managed through /auth."
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        """Presets are conveniences; arbitrary model names remain editable."""
        event.stop()
        if (
            event.select.id != "decision-preset"
            or self._applying
            or not self._loaded
            or event.value == self._preset
        ):
            return
        self._preset = str(event.value)
        if event.value == "jev":
            self.query_one("#decision-endpoint", Input).value = NovaConfig.TOOL_VERDICT_JEV_ENDPOINT
            self.query_one("#decision-model", Input).value = NovaConfig.TOOL_VERDICT_JEV_MODEL
        elif event.value == "tev1":
            self.query_one(
                "#decision-endpoint", Input
            ).value = NovaConfig.TOOL_VERDICT_DEFAULT_ENDPOINT
            self.query_one("#decision-model", Input).value = NovaConfig.TOOL_VERDICT_DEFAULT_MODEL

    def _hint(self, message: str, *, error: bool = False) -> None:
        self.query_one("#decision-status", Static).update(
            Text(message, style="red" if error else "dim")
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Keep credential management inside this tab's staged workflow."""
        if event.button.id == "decision-auth":
            event.stop()
            self.open_auth()

    @work(exclusive=True, group="decision-auth")
    async def open_auth(self) -> None:
        """Open `/auth` without losing the draft model, endpoint or toggle."""
        from novacode_cli.tui.auth_screens import AuthManagerScreen

        endpoint = self.query_one("#decision-endpoint", Input).value.strip()
        provider = "jev" if endpoint == NovaConfig.TOOL_VERDICT_JEV_ENDPOINT else "systemone"
        await self.app.push_screen_wait(AuthManagerScreen(focus=provider))

    @work(exclusive=True, group="decision-save")
    async def save(self) -> None:
        """Validate credentials when enabled, then save settings atomically."""
        from novacode_cli.config.credentials import credential_value

        if not self._loaded:
            self._hint("Decision settings are still loading.", error=True)
            return
        enabled = self.query_one("#decision-enabled", Switch).value
        endpoint = self.query_one("#decision-endpoint", Input).value.strip()
        model = self.query_one("#decision-model", Input).value.strip()
        if (
            enabled
            and endpoint == NovaConfig.TOOL_VERDICT_JEV_ENDPOINT
            and not await asyncio.to_thread(credential_value, "TYPESAFE_API_KEY")
        ):
            self._hint(
                "Add a Jev API key with Manage API key or /auth before enabling Jev.", error=True
            )
            return
        try:
            await asyncio.to_thread(self._persist, enabled=enabled, endpoint=endpoint, model=model)
        except ValueError as error:
            self._hint(str(error), error=True)
            return
        except OSError:
            self._hint("Could not save decision settings. Try again.", error=True)
            return
        self.post_message(self.Saved(enabled=enabled, model=model))

    @staticmethod
    def _persist(*, enabled: bool, endpoint: str, model: str) -> None:
        NovaConfig().set_tool_verdict_settings(enabled=enabled, endpoint=endpoint, model=model)
