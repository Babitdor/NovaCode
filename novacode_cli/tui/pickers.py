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



class SessionPickerApp(App[str | None]):
    """Pick a saved session to resume. Returns the chosen ``session_id`` or None."""

    CSS = """
    Screen { align: center middle; background: $surface; }
    #box {
        width: 90%; max-width: 120; height: auto; max-height: 90%;
        border: thick $accent; background: $panel; padding: 1 4;
    }
    #title { text-style: bold; color: $accent; margin-bottom: 1; }
    #sessions { height: auto; max-height: 70%; }
    #hint { color: $text-muted; margin-top: 1; }
    #buttons { height: auto; align: center middle; margin-top: 1; }
    #buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
        Binding("enter", "resume", "Resume", show=False),
    ]

    def __init__(self, sessions: list[Any]) -> None:
        super().__init__()
        self._sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(Text("Resume a session"), id="title")
            yield OptionList(id="sessions")
            yield Static(
                Text("↑/↓ select · Enter resume · Esc cancel", style="dim"), id="hint"
            )
        yield Footer()

    def on_mount(self) -> None:
        ol = self.query_one("#sessions", OptionList)
        for meta in self._sessions:
            ol.add_option(Option(self._fmt(meta)))
        if self._sessions:
            ol.highlighted = 0
        ol.focus()

    @staticmethod
    def _fmt(meta: Any) -> Text:
        from pathlib import Path

        from novacode_cli.session.session_restore import (
            _truncate,
            format_session_age,
        )

        age = format_session_age(meta.last_active)
        project = Path(meta.project_root).name if meta.project_root else "(no project)"
        model = meta.model_name or "unknown"
        # Legacy sessions (no recorded provider) cannot have their model rebuilt
        # on resume — mark them so the label never promises a restore.
        if meta.model_name and not getattr(meta, "model_provider", None):
            model = f"{model} (not restored)"
        task = _truncate(getattr(meta, "current_task", None), 40) or "—"
        t = Text()
        t.append(f"{meta.session_id[:8]}  ", style="bold cyan")
        t.append(f"{project} ", style="white")
        t.append(f"({model})  ·  ", style="dim")
        t.append(f"{meta.message_count} msgs  ·  {age}", style="dim")
        # Plain-text status (the rich-markup helper returns markup, not for OptionList).
        t.append(f"  ·  {task}", style="dim")
        return t

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._sessions):
            self.exit(self._sessions[idx].session_id)

    def action_resume(self) -> None:
        ol = self.query_one("#sessions", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._sessions):
            self.exit(self._sessions[idx].session_id)

    def action_cancel(self) -> None:
        self.exit(None)


async def pick_session_tui(sessions: list[Any]) -> str | None:
    """Run the session picker as a standalone Textual app; return the choice."""
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
                yield Button("Cancel", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.exit(False)
        elif event.button.id == "finish":
            self._finish()

    @work
    async def _finish(self) -> None:
        provider = str(self.query_one("#provider", Select).value)
        key = self.query_one("#provider-key", Input).value.strip()
        status = self.query_one("#status", Static)
        if provider != "ollama" and not key:
            status.update(Text(f"{provider.title()} API key is required.", style="red"))
            return
        status.update(Text("Saving…", style="dim"))
        opt = {
            "tavily": self.query_one("#tavily", Input).value.strip(),
        }
        try:
            await asyncio.to_thread(self._persist, provider, key, opt)
        except Exception as ex:  # noqa: BLE001
            status.update(Text(f"Setup failed: {ex}", style="red"))
            return
        self.exit(True)

    @staticmethod
    def _persist(provider: str, key: str, opt: dict[str, str]) -> None:
        from novacode_cli.onboarding import API_KEY_NAMES, OnboardingWizard

        wizard = OnboardingWizard()
        if provider == "ollama":
            provider_config = {"host": key or "http://localhost:11434"}
        else:
            wizard.secret_manager.store_secret(API_KEY_NAMES[provider], key)
            provider_config = {"api_key": key}
        for name, value in opt.items():
            if value:
                wizard.secret_manager.store_secret(API_KEY_NAMES[name], value)
        wizard._save_config(provider, provider_config, opt.get("tavily") or None)

    def action_cancel(self) -> None:
        self.exit(False)


def run_onboarding_tui() -> bool:
    """Run onboarding as a standalone Textual app (sync); True on success."""
    return bool(OnboardingApp().run())
