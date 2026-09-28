"""The ``/auth`` screens: badges, the key prompt, and deletion.

Driven through the real credential store with a stubbed ``SecretManager``, so
these cover the wiring the screen actually uses: status resolution decides the
badge, a save reaches the keychain and the environment, and a delete reaches
both plus the recorded metadata.
"""

from __future__ import annotations

import os

import pytest

from novacode_cli.config.provider_auth import credential_names

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual is not installed")

OPENAI = "OPENAI_API_KEY"
ANTHROPIC = "ANTHROPIC_API_KEY"


class _StubSecretManager:
    """SecretManager stand-in: a dict, so no real keychain is touched."""

    store: dict[str, str] = {}
    deleted: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_secret(self, name: str) -> str | None:
        return type(self).store.get(name)

    def store_secret(self, name: str, value: str) -> bool:
        type(self).store[name] = value
        return True

    def delete_secret(self, name: str) -> bool:
        type(self).deleted.append(name)
        type(self).store.pop(name, None)
        return True


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    """Isolate config and credential storage for the whole test."""
    import novacode_cli.config.config as config_mod
    from novacode_cli.config.model_manager import MODEL_PRESETS

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _StubSecretManager)
    _StubSecretManager.store = {}
    _StubSecretManager.deleted = []
    for preset in MODEL_PRESETS.values():
        var = preset.get("api_key_var")
        if var:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return _StubSecretManager


async def _settle(pilot, predicate, *, tries: int = 120) -> bool:
    """Pause until *predicate* holds, so a background worker can land."""
    for _ in range(tries):
        await pilot.pause()
        if predicate():
            return True
    return False


def _rows(screen) -> list[str | None]:
    from textual.widgets import OptionList

    option_list = screen.query_one("#auth-list", OptionList)
    return [option.id for option in option_list.options]


def _labels(screen) -> list[str]:
    from textual.widgets import OptionList

    option_list = screen.query_one("#auth-list", OptionList)
    return [str(option.prompt) for option in option_list.options]


# ---------------------------------------------------------------------------
# The manager list
# ---------------------------------------------------------------------------


async def test_the_manager_lists_every_storable_credential(secrets):
    from textual.app import App

    from novacode_cli.tui.auth_screens import AuthManagerScreen

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        manager = AuthManagerScreen()
        app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))

        assert set(_rows(manager)) == set(credential_names())
        # Nothing is configured in this fixture, so every row says so.
        joined = " ".join(_labels(manager))
        assert "[missing]" in joined


async def test_a_stored_key_is_reported_as_stored(secrets):
    from textual.app import App

    from novacode_cli.tui.auth_screens import AuthManagerScreen

    secrets.store = {"anthropic_api_key": "sk-stored"}

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        manager = AuthManagerScreen()
        app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))

        labels = dict(zip(_rows(manager), _labels(manager), strict=True))
        assert "[stored]" in labels["anthropic"]
        assert "[missing]" in labels["openai"]


async def test_the_voice_providers_are_manageable_here_too(secrets):
    """Deepgram and ElevenLabs are credentials like any other.

    They used to be storable only through `/voice settings --key`, which wrote
    them to the config file in plaintext.
    """
    from textual.app import App

    from novacode_cli.tui.auth_screens import AuthManagerScreen

    secrets.store = {"elevenlabs_api_key": "el-stored"}

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        manager = AuthManagerScreen()
        app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))

        rows = _rows(manager)
        labels = dict(zip(rows, _labels(manager), strict=True))

        assert "deepgram" in rows
        assert "elevenlabs" in rows
        assert "[stored]" in labels["elevenlabs"]
        assert "[missing]" in labels["deepgram"]
        # The name has to say which kind of provider it is: a bare "Deepgram"
        # next to "OpenAI" reads like a chat provider.
        assert "speech to text" in labels["deepgram"]
        assert "text to speech" in labels["elevenlabs"]


# ---------------------------------------------------------------------------
# The key prompt
# ---------------------------------------------------------------------------


async def test_entering_a_key_stores_it_and_exports_it(secrets):
    from textual.app import App
    from textual.widgets import Input

    from novacode_cli.tui.auth_screens import AuthManagerScreen

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        manager = AuthManagerScreen(focus="openai")
        app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))

        await pilot.press("enter")  # opens the prompt for the highlighted row
        assert await _settle(pilot, lambda: app.screen is not manager)

        prompt = app.screen
        prompt.query_one("#auth-key", Input).value = "sk-typed"
        await pilot.press("enter")
        assert await _settle(pilot, lambda: app.screen is manager)

        assert secrets.store["openai_api_key"] == "sk-typed"
        assert os.environ[OPENAI] == "sk-typed"
        assert manager.saved == ["openai"]


async def test_an_empty_key_is_refused(secrets):
    from textual.app import App
    from textual.widgets import Input, Static

    from novacode_cli.tui.auth_screens import AuthPromptScreen

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = AuthPromptScreen("openai")
        app.push_screen(prompt)
        await pilot.pause()

        await pilot.press("enter")

        assert secrets.store == {}
        error = str(prompt.query_one("#auth-error", Static).content)
        assert "empty" in error
        # Still open: a refused save must not look like a cancelled one.
        assert app.screen is prompt
        assert prompt.query_one("#auth-key", Input).value == ""


async def test_the_endpoint_field_is_offered_only_where_it_applies(secrets):
    from textual.app import App
    from textual.widgets import Input

    from novacode_cli.tui.auth_screens import AuthPromptScreen

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        for provider, expected in (("openai", True), ("anthropic", False)):
            prompt = AuthPromptScreen(provider)
            app.push_screen(prompt)
            await pilot.pause()

            assert not prompt.query_one("#auth-endpoint", Input).display
            await pilot.press("f2")

            assert prompt.query_one("#auth-endpoint", Input).display is expected
            prompt.dismiss(None)
            await pilot.pause()


async def test_a_saved_endpoint_is_paired_with_the_key(secrets):
    from textual.app import App
    from textual.widgets import Input

    from novacode_cli.config.credentials import credential_meta
    from novacode_cli.tui.auth_screens import AuthPromptScreen

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = AuthPromptScreen("openai")
        app.push_screen(prompt)
        await pilot.pause()

        prompt.query_one("#auth-key", Input).value = "sk-typed"
        await pilot.press("f2")
        prompt.query_one("#auth-endpoint", Input).value = "http://localhost:1234/v1"
        await pilot.press("enter")
        await pilot.pause()

        meta = credential_meta(OPENAI)
        assert meta is not None
        assert meta.base_url == "http://localhost:1234/v1"
        assert os.environ["OPENAI_BASE_URL"] == "http://localhost:1234/v1"


async def test_a_non_http_endpoint_is_rejected(secrets):
    from textual.app import App
    from textual.widgets import Input, Static

    from novacode_cli.config.credentials import credential_meta
    from novacode_cli.tui.auth_screens import AuthPromptScreen

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = AuthPromptScreen("openai")
        app.push_screen(prompt)
        await pilot.pause()

        prompt.query_one("#auth-key", Input).value = "sk-typed"
        await pilot.press("f2")
        prompt.query_one("#auth-endpoint", Input).value = "ftp://localhost/v1"
        await pilot.press("enter")
        await pilot.pause()

        assert secrets.store == {}
        assert credential_meta(OPENAI) is None
        assert "http(s)" in str(prompt.query_one("#auth-error", Static).content)


async def test_a_legacy_bad_endpoint_does_not_block_rotating_the_key(secrets):
    """The field is prefilled with the stored value; an unchanged value is left alone."""
    from textual.app import App
    from textual.widgets import Input

    from novacode_cli.config.credentials import credential_meta
    from novacode_cli.tui.auth_screens import AuthPromptScreen

    secrets.store = {"openai_api_key": "sk-old"}
    from novacode_cli.config.credentials import set_credential

    set_credential(OPENAI, "sk-old", base_url="localhost:1234")

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = AuthPromptScreen("openai", existing=credential_meta(OPENAI))
        app.push_screen(prompt)
        await pilot.pause()

        prompt.query_one("#auth-key", Input).value = "sk-new"
        await pilot.press("enter")
        await pilot.pause()

        assert secrets.store["openai_api_key"] == "sk-new"


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


async def test_delete_requires_confirmation(secrets):
    from textual.app import App

    from novacode_cli.tui.auth_screens import AuthManagerScreen
    from novacode_cli.tui.screens import ConfirmModal

    secrets.store = {"anthropic_api_key": "sk-stored"}

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        manager = AuthManagerScreen(focus="anthropic")
        app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))

        await pilot.press("ctrl+d")
        assert await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))

        await pilot.press("n")  # decline
        assert await _settle(pilot, lambda: app.screen is manager)
        assert secrets.store["anthropic_api_key"] == "sk-stored"
        assert manager.deleted == []

        await pilot.press("ctrl+d")
        assert await _settle(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        assert await _settle(pilot, lambda: app.screen is manager)

        assert "anthropic_api_key" not in secrets.store
        assert secrets.deleted == ["anthropic_api_key"]
        assert manager.deleted == ["anthropic"]


async def test_an_environment_supplied_key_is_not_deleted_from_here(secrets, monkeypatch):
    """`/auth` cannot unset a shell variable, so it must say so, not pretend."""
    from textual.app import App

    from novacode_cli.tui.auth_screens import AuthManagerScreen
    from novacode_cli.tui.screens import ConfirmModal

    monkeypatch.setenv(ANTHROPIC, "sk-from-shell")

    class _Host(App):
        def compose(self):
            return []

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        manager = AuthManagerScreen(focus="anthropic")
        app.push_screen(manager)
        assert await _settle(pilot, lambda: bool(_rows(manager)))

        await pilot.press("ctrl+d")
        await pilot.pause()

        assert not isinstance(app.screen, ConfirmModal)
        assert os.environ[ANTHROPIC] == "sk-from-shell"
        assert manager.deleted == []
