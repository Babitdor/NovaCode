"""Documented provider sign-in choices and responsive Google OAuth setup."""

from __future__ import annotations

import asyncio
import threading
import webbrowser
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Link, Static

from novacode_cli.config import google_oauth_auth as google


class ProviderSignInChoice(ModalScreen[str | None]):
    """Offer only sign-in methods supported for third-party apps."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ProviderSignInChoice #signin-options { width: 90%; max-width: 80; height: auto;
        max-height: 90%; border: round $accent; padding: 1 2; background: $surface; }
    ProviderSignInChoice Button { width: 100%; margin-top: 1; }
    """

    def __init__(self, provider: str) -> None:
        super().__init__()
        self.provider = provider

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="signin-options"):
            if self.provider in {"opencode", "opencode_zen"}:
                name = "OpenCode Zen" if self.provider == "opencode_zen" else "OpenCode Go"
                yield Static(f"Connect {name}")
                yield Static(
                    "Sign in to OpenCode Console in your browser, then copy an API key.\n"
                    + ("Add Zen credits or choose an available free model." if self.provider == "opencode_zen"
                       else "Subscribe to Go or Go Plus to use its models.")
                    + "\nReturn to Nova and paste the key in the masked field. "
                    "Opening the Console alone does not connect Nova."
                )
                yield Button("Sign in to OpenCode Console", id="opencode-console", variant="primary")
                yield Button("Enter API key", id="provider-replace")
                yield Link("OpenCode setup guide", url=f"https://opencode.ai/docs/{'zen' if self.provider == 'opencode_zen' else 'go'}/")
            elif self.provider == "google":
                yield Static("Connect Gemini")
                yield Static(
                    "Sign in with Google using your Desktop OAuth client.\n"
                    "Uses Gemini API project quotas and billing."
                )
                yield Button("Sign in with Google", id="google-signin", variant="primary")
                if google.has_credentials():
                    yield Button("Use saved Google sign-in", id="google-use")
                    yield Button("Disconnect Google sign-in", id="google-remove", variant="error")
                yield Button("Use API key", id="provider-key")
                yield Button("Add / replace API key", id="provider-replace")
            else:
                yield Static("Connect Anthropic")
                yield Static(
                    "Sign in to the Anthropic Console to create an API key.\n"
                    "Claude subscription sign-in is restricted to Anthropic apps."
                )
                yield Button("Open Anthropic Console", id="anthropic-console", variant="primary")
                yield Button("Enter API key", id="provider-replace")
                yield Link(
                    "Supported authentication methods",
                    url="https://platform.claude.com/docs/en/manage-claude/authentication",
                )
            yield Button("Cancel", id="provider-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "google-signin": "signin",
            "google-use": "use",
            "google-remove": "remove",
            "provider-key": "api_key",
            "provider-replace": "replace_key",
            "anthropic-console": "console",
            "opencode-console": "console",
        }
        self.dismiss(choices.get(event.button.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class GoogleSignInScreen(ModalScreen[bool]):
    """Keep browser/network work off the TUI thread and cancel on dismissal."""

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    GoogleSignInScreen #google-setup { width: 90%; max-width: 90; height: auto;
        max-height: 90%; border: round $accent; padding: 1 2; background: $surface; }
    GoogleSignInScreen Button { width: 100%; margin-top: 1; }
    GoogleSignInScreen Static { height: auto; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.cancel = threading.Event()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="google-setup"):
            yield Static("Sign in with Google · Gemini API")
            yield Static(
                "Enable the Generative Language API in your Google Cloud project. "
                "Configure OAuth consent, then download a Desktop app client JSON. "
                "For a testing app, add your Google account as a test user."
            )
            yield Link("Google's setup guide", url="https://ai.google.dev/gemini-api/docs/oauth")
            yield Input(placeholder="Desktop OAuth client JSON file path", id="google-client-file")
            yield Input(placeholder="Google Cloud project ID", id="google-project")
            yield Static(
                "Your Google browser sign-in authorizes API access for this project. "
                "Gemini CLI subscription quotas do not apply."
            )
            yield Static("", id="google-progress")
            yield Link(
                "Open sign-in page", url="https://accounts.google.com", id="google-login-url"
            )
            yield Button("Continue with Google", id="google-connect", variant="primary")
            yield Button("Cancel", id="google-cancel")

    def on_mount(self) -> None:
        self.query_one("#google-login-url").display = False
        self.query_one(Input).focus()

    def _show_url(self, url: str) -> None:
        if self.is_mounted and not self.cancel.is_set():
            link = self.query_one("#google-login-url", Link)
            link.url = url
            link.display = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "google-cancel":
            self.action_cancel()
        elif event.button.id == "google-connect":
            self.connect()

    @work(exclusive=True, exit_on_error=False)
    async def connect(self) -> None:
        path = Path(self.query_one("#google-client-file", Input).value.strip().strip('"'))
        project = self.query_one("#google-project", Input).value.strip()
        self.query_one("#google-connect", Button).disabled = True
        self.query_one("#google-progress", Static).update("Waiting for Google browser sign-in…")
        try:

            def url_ready(url: str) -> None:
                if not self.cancel.is_set():
                    self.app.call_from_thread(self._show_url, url)

            record = await asyncio.to_thread(
                google.sign_in, path, project, self.cancel, on_url=url_ready
            )
            if self.cancel.is_set() or not self.is_mounted:
                return
            await asyncio.to_thread(google.save_credentials, record)
            google.set_auth_mode("oauth")
            self.dismiss(True)
        except Exception:  # noqa: BLE001 — provider/library exceptions can contain secrets
            if self.is_mounted and not self.cancel.is_set():
                self.query_one("#google-progress", Static).update(
                    "Could not connect. Check the Desktop client file, project ID, consent "
                    "and network, then retry. Sign-in requires a working credential store."
                )
                self.query_one("#google-connect", Button).disabled = False

    def action_cancel(self) -> None:
        self.cancel.set()
        self.dismiss(False)

    def on_unmount(self) -> None:
        self.cancel.set()


async def open_anthropic_console() -> None:
    """Open the documented key-creation page without handling login cookies."""
    await asyncio.to_thread(webbrowser.open, "https://platform.claude.com/settings/keys")


async def open_opencode_console() -> None:
    """Open the documented Console login; credential entry remains explicit."""
    await asyncio.to_thread(webbrowser.open, "https://opencode.ai/auth")
