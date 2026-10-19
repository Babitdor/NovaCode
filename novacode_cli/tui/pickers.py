"""Standalone Textual screens used before the main chat TUI starts.

These run as their own tiny ``App`` (via ``run_async``) for flows that happen
*before* the agent/chat app exists — currently the ``--resume`` session picker.
Each app returns its result through ``App.exit(value)``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, OptionList, Select, Static
from textual.widgets.option_list import Option



class SessionPickerApp(App[tuple[str, bool] | None]):
    """``nova --resume``: the chat's /resume screens, before the chat exists.

    The same two questions as /resume in the chat, drawn by the same screens
    and stylesheet in the user's theme: which session, then whether to compact
    it first. Returns ``(session_id, compact)``, or None on cancel.
    """

    TITLE = "NovaCode"

    def __init__(self, sessions: list[Any]) -> None:
        from novacode_cli.tui.app import NovaApp

        # The chat's stylesheet, so the screens look as they do in the chat.
        type(self).CSS = NovaApp.CSS
        super().__init__()
        self._sessions = sessions
        self._session_id: str | None = None
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli.tui.home import home_style

        config = NovaConfig()
        self._home_style = home_style(
            config.get("home_style"), legacy_dashboard=config.get("home_dashboard_enabled", True)
        )

    def on_mount(self) -> None:
        from novacode_cli.tui.app import NovaApp
        from novacode_cli.tui.resume_picker import ResumeSessionScreen

        NovaApp._apply_saved_theme(self)  # type: ignore[arg-type]
        screen = ResumeSessionScreen(self._sessions)
        screen.add_class(f"{self._home_style}-ui")
        self.push_screen(screen, self._picked)

    def _picked(self, idx: int | None) -> None:
        from novacode_cli.tui.resume_picker import ResumeModeScreen

        if idx is None or not 0 <= idx < len(self._sessions):
            self.exit(None)
            return
        self._session_id = self._sessions[idx].session_id
        screen = ResumeModeScreen()
        screen.add_class(f"{self._home_style}-ui")
        self.push_screen(screen, self._chosen)

    def _chosen(self, choice: int | None) -> None:
        self.exit((self._session_id, choice == 0) if choice in (0, 1) else None)


async def pick_session_tui(sessions: list[Any]) -> tuple[str, bool] | None:
    """Run the --resume picker; ``(session_id, compact)`` or None."""
    return await SessionPickerApp(sessions).run_async()


class OnboardingApp(App[bool]):
    """First-run setup as a native Textual screen. Returns True on success.

    Collects the LLM provider + its host/key and optional service keys, then
    persists via the existing OnboardingWizard (SecretManager + _save_config),
    so storage behavior matches the legacy wizard exactly.
    """

    CSS = """
    Screen { align: center middle; background: $surface; }
    #box {
        width: 90%; max-width: 100; height: auto; max-height: 95%;
        border: thick $accent; background: $panel; padding: 1 4;
    }
    #title { text-style: bold; color: $accent; margin-bottom: 1; }
    .lbl { margin-top: 1; }
    #status { margin-top: 1; }
    #buttons { height: auto; align: center middle; margin-top: 1; }
    #buttons Button { margin: 0 1; }
    Select, Input { margin-top: 0; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        from novacode_cli.onboarding import OnboardingWizard

        provider_opts = [
            (v["display"], v["name"]) for v in OnboardingWizard.PROVIDERS.values()
        ]
        with VerticalScroll(id="box"):
            yield Static(Text("Welcome to Nova 👋  —  first-time setup"), id="title")
            yield Static(Text("LLM provider", style="bold"), classes="lbl")
            yield Select(provider_opts, value="ollama", allow_blank=False, id="provider")
            yield Static("", id="key-label", classes="lbl")
            yield Input(id="provider-key")
            yield Static(
                Text("Optional keys — leave blank to skip", style="dim"), classes="lbl"
            )
            yield Input(placeholder="Tavily API key (web search)", password=True, id="tavily")
            yield Static("", id="status")
            with Horizontal(id="buttons"):
                yield Button("Finish", id="finish", variant="success")
                # Shown only after a check that did not pass: being offline, or
                # setting up before the server is started, are both legitimate.
                yield Button("Save anyway", id="save-anyway")
                yield Button("Cancel", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#save-anyway", Button).display = False
        self._sync_key_field("ollama")
        self.query_one("#provider", Select).focus()

    def _sync_key_field(self, provider: str) -> None:
        lbl = self.query_one("#key-label", Static)
        inp = self.query_one("#provider-key", Input)
        if provider == "ollama":
            lbl.update(Text("Ollama host", style="bold"))
            inp.password = False
            inp.placeholder = "http://localhost:11434"
        else:
            lbl.update(Text(f"{provider.title()} API key", style="bold"))
            inp.password = True
            inp.placeholder = f"{provider} API key (required)"

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider" and event.value is not Select.BLANK:
            self._sync_key_field(str(event.value))
            self._offer_save_anyway(False)  # a different provider is a fresh attempt

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "provider-key":
            self._offer_save_anyway(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._finish()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.exit(False)
        elif event.button.id == "finish":
            self._finish()
        elif event.button.id == "save-anyway":
            self._finish(verify=False)

    def _offer_save_anyway(self, show: bool) -> None:
        self.query_one("#save-anyway", Button).display = show

    @work(exclusive=True)
    async def _finish(self, verify: bool = True) -> None:
        """Check what was entered, then save it.

        Saving used to be unconditional, so a mistyped key or an Ollama host
        with nothing behind it ended in "setup complete" and failed on the
        first prompt instead. The check is one request; when it does not pass
        the reason is shown and "Save anyway" appears.
        """
        from novacode_cli.config.model_manager import MODEL_PRESETS
        from novacode_cli.onboarding_check import (
            DEFAULT_OLLAMA_HOST,
            check_provider,
            pick_ollama_model,
        )

        provider = str(self.query_one("#provider", Select).value)
        key = self.query_one("#provider-key", Input).value.strip()
        status = self.query_one("#status", Static)
        if provider == "ollama":
            key = key or DEFAULT_OLLAMA_HOST
        elif not key:
            status.update(Text(f"{provider.title()} API key is required.", style="red"))
            return

        model = str((MODEL_PRESETS.get(provider) or {}).get("default_model") or "")
        if verify:
            status.update(Text("Checking…", style="dim"))
            check = await asyncio.to_thread(check_provider, provider, key)
            if check.ok is not True:
                status.update(Text(check.message, style="red" if check.ok is False else "yellow"))
                self._offer_save_anyway(True)
                return
            if provider == "ollama":
                model = pick_ollama_model(check.models)

        status.update(Text("Saving…", style="dim"))
        opt = {
            "tavily": self.query_one("#tavily", Input).value.strip(),
        }
        try:
            await asyncio.to_thread(self._persist, provider, key, opt, model)
        except Exception as ex:  # noqa: BLE001
            status.update(Text(f"Setup failed: {ex}", style="red"))
            return
        self.exit(True)

    @staticmethod
    def _persist(provider: str, key: str, opt: dict[str, str], model: str = "") -> None:
        from novacode_cli.onboarding import API_KEY_NAMES, OnboardingWizard

        wizard = OnboardingWizard()
        if provider == "ollama":
            provider_config = {"host": key or "http://localhost:11434"}
        else:
            if not wizard.secret_manager.store_secret(API_KEY_NAMES[provider], key):
                msg = "the API key could not be stored"
                raise RuntimeError(msg)
            provider_config = {"api_key": key}
        for name, value in opt.items():
            if value:
                wizard.secret_manager.store_secret(API_KEY_NAMES[name], value)
        wizard._save_config(provider, provider_config, opt.get("tavily") or None, model or None)

    def action_cancel(self) -> None:
        self.exit(False)


def run_onboarding_tui() -> bool:
    """Run onboarding as a standalone Textual app (sync); True on success."""
    return bool(OnboardingApp().run())
