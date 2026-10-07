"""Browser-assisted provider auth, account selection and cancellation."""

import threading

import pytest
from textual.app import App
from textual.widgets import Input, Static

from novacode_cli.config import google_oauth_auth as google
from novacode_cli.tui.auth_screens import AuthManagerScreen, AuthPromptScreen
from novacode_cli.tui.provider_signin import GoogleSignInScreen, ProviderSignInChoice
from tests.test_auth_screens import _rows, _settle
from tests.test_google_oauth_auth import Secrets, record
from tests.test_tui_sessions import isolated_session_config  # noqa: F401


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    Secrets.values = {}
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", Secrets)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_anthropic_console_is_guided_key_setup(monkeypatch):
    urls = []
    monkeypatch.setattr("webbrowser.open", lambda url: urls.append(url))
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        manager = AuthManagerScreen(focus="anthropic")
        await app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))
        manager.open_prompt()
        assert await _settle(pilot, lambda: isinstance(app.screen, ProviderSignInChoice))
        assert "restricted" in str(app.screen.query(Static)[1].content)
        await pilot.click("#anthropic-console")
        assert await _settle(pilot, lambda: isinstance(app.screen, AuthPromptScreen))
        assert urls == ["https://platform.claude.com/settings/keys"]
        assert not Secrets.values


@pytest.mark.asyncio
async def test_google_signin_from_manager_then_select_existing_api_key(monkeypatch):
    seen = []

    def sign_in(path, project, cancel, **_kwargs):
        seen.append((str(path), project, cancel.is_set()))
        return record()

    monkeypatch.setattr(google, "sign_in", sign_in)
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        manager = AuthManagerScreen(focus="google")
        await app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))
        manager.open_prompt()
        assert await _settle(pilot, lambda: isinstance(app.screen, ProviderSignInChoice))
        await pilot.click("#google-signin")
        assert await _settle(pilot, lambda: isinstance(app.screen, GoogleSignInScreen))
        app.screen.query_one("#google-client-file", Input).value = "desktop.json"
        app.screen.query_one("#google-project", Input).value = "test-project"
        await pilot.click("#google-connect")
        assert await _settle(pilot, lambda: app.screen is manager)
        assert google.has_credentials()
        assert google.selected_auth_mode() == "oauth"
        assert seen == [("desktop.json", "test-project", False)]
        Secrets.values["google_api_key"] = "existing-key"
        manager.open_prompt()
        assert await _settle(pilot, lambda: isinstance(app.screen, ProviderSignInChoice))
        await pilot.click("#provider-key")
        assert await _settle(pilot, lambda: app.screen is manager)
        assert google.selected_auth_mode() == "api_key"
        assert google.has_credentials()


@pytest.mark.asyncio
async def test_cancelled_google_screen_does_not_save_late_completion(monkeypatch):
    entered = threading.Event()
    finished = threading.Event()

    def sign_in(_path, _project, cancel, **_kwargs):
        entered.set()
        cancel.wait(3)
        finished.set()
        return record()

    monkeypatch.setattr(google, "sign_in", sign_in)
    app = App()
    async with app.run_test(size=(60, 22)) as pilot:
        screen = GoogleSignInScreen()
        await app.push_screen(screen)
        screen.connect()
        assert await _settle(pilot, entered.is_set)
        await pilot.press("escape")
        assert await _settle(pilot, finished.is_set)
        assert not google.has_credentials()


@pytest.mark.asyncio
async def test_google_error_never_echoes_provider_secret(monkeypatch):
    def failed(*_args, **_kwargs):
        raise RuntimeError("refresh-secret provider response")

    monkeypatch.setattr(google, "sign_in", failed)
    app = App()
    async with app.run_test(size=(80, 30)) as pilot:
        screen = GoogleSignInScreen()
        await app.push_screen(screen)
        screen.connect()
        assert await _settle(
            pilot,
            lambda: "Could not connect"
            in str(screen.query_one("#google-progress", Static).content),
        )
        assert "refresh-secret" not in str(screen.query_one("#google-progress", Static).content)
        assert not google.has_credentials()
