"""Executable acceptance criteria for Zen and guided OpenCode Console sign-in.

Run: python -m pytest tests/test_opencode_zen.py
Spec approval: not obtained (autonomous run authorized by the implementation request).
Failure model: wrong gateway/key pairing, leaked keys, unsupported protocol,
browser login falsely reported as connected, malformed model discovery.
Existing secret storage and Go provider IDs must remain compatible.
Setup: existing pytest/Textual/HTTP clients only; no new dependencies or git mutations.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from novacode_cli.config import model_create as mc
from novacode_cli.config import model_manager as mm
from novacode_cli.config.provider_auth import credential_env_var
from tests.test_auth_screens import secrets  # noqa: F401


def test_zen_and_go_have_separate_provider_credentials():
    assert credential_env_var("opencode_zen") == "OPENCODE_ZEN_API_KEY"
    assert credential_env_var("opencode") == "OPENCODE_API_KEY"
    assert mm.MODEL_PRESETS["opencode_zen"]["base_url"] == "https://opencode.ai/zen/v1"
    assert mm.MODEL_PRESETS["opencode"]["base_url"] == "https://opencode.ai/zen/go/v1"


@pytest.mark.parametrize(
    "provider,base,var",
    [
        ("opencode", "https://opencode.ai/zen/go/v1/", "OPENCODE_API_KEY"),
        ("opencode_zen", "https://opencode.ai/zen/v1/", "OPENCODE_ZEN_API_KEY"),
    ],
)
def test_gateway_models_send_only_their_key_and_session_header(monkeypatch, provider, base, var):
    monkeypatch.setenv("OPENCODE_API_KEY", "go-test-key")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "zen-test-key")
    monkeypatch.setattr(mc.settings, "opencode_api_key", None)
    monkeypatch.setattr(mc.settings, "opencode_zen_api_key", None, raising=False)
    model = mc.create_model_from_config(provider, "deepseek-v4.1-flash")
    assert model is not None
    assert str(model.root_client.base_url) == base
    assert model.openai_api_key.get_secret_value() == (
        "go-test-key" if var == "OPENCODE_API_KEY" else "zen-test-key"
    )
    assert model.root_client.default_headers["x-opencode-session"] == mc.opencode_session_id()
    assert "novacode" in model.root_client.default_headers["User-Agent"].lower()


def test_go_key_does_not_unlock_zen(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "go-key")
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.setattr(mc.settings, "opencode_zen_api_key", None, raising=False)
    assert mc.create_model_from_config("opencode_zen", "big-pickle") is None


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("glm-5.3", "chat"),
        ("gpt-5.4", "responses"),
        ("claude-sonnet-4-6", "messages"),
        ("minimax-m3", "chat"),
        ("qwen3.8-max", "messages"),
        ("jev-1.13", "unsupported"),
        ("gemini-3.1-pro", "unsupported"),
    ],
)
def test_zen_selects_documented_protocol(model_id, expected):
    from novacode_cli.config import opencode_gateway

    assert opencode_gateway.protocol("opencode_zen", model_id) == expected


def test_zen_live_discovery_filters_non_chat_agents_and_keeps_supported_protocols(monkeypatch):
    captured = {}

    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "data": [
                    {"id": "big-pickle"},
                    {"id": "gpt-5.4"},
                    {"id": "claude-sonnet-4-6"},
                    {"id": "jev-1.13"},
                    {"id": "gemini-3.1-pro"},
                    {"id": 123},
                ]
            },
        )

    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(
        mm.Settings, "from_environment", lambda: SimpleNamespace(opencode_zen_api_key="zen-key")
    )
    assert mm.get_opencode_models("opencode_zen") == ["big-pickle", "claude-sonnet-4-6", "gpt-5.4"]
    assert captured["url"] == "https://opencode.ai/zen/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer zen-key"


@pytest.mark.parametrize("provider", ["opencode", "opencode_zen"])
async def test_console_signin_choice_is_guided_key_entry(provider, secrets):
    from textual.app import App
    from textual.widgets import Button, Static

    from novacode_cli.tui.provider_signin import ProviderSignInChoice

    app = App()
    async with app.run_test(size=(90, 32)) as pilot:
        screen = ProviderSignInChoice(provider)
        app.push_screen(screen)
        await pilot.pause()
        assert (
            screen.query_one("#opencode-console", Button).label.plain
            == "Sign in to OpenCode Console"
        )
        content = " ".join(str(widget.render()) for widget in screen.query(Static))
        assert "API key" in content
        assert "OpenCode" in content
        assert secrets.store == {}


async def test_console_open_uses_fixed_official_url_without_storing_auth(monkeypatch, secrets):
    from novacode_cli.tui import provider_signin

    browser = Mock(return_value=True)
    monkeypatch.setattr(provider_signin.webbrowser, "open", browser)
    await provider_signin.open_opencode_console()
    browser.assert_called_once_with("https://opencode.ai/auth")
    assert secrets.store == {}


@pytest.mark.parametrize(
    "provider,model_id",
    [
        ("opencode_zen", "claude-sonnet-4-6"),
        ("opencode", "minimax-m3"),
        ("opencode_zen", "qwen3.8-max"),
    ],
)
def test_gateway_messages_client_uses_its_own_key_and_base(monkeypatch, provider, model_id):
    monkeypatch.setattr(mc.settings, f"{provider}_api_key", "isolated-test-key")
    model = mc.build_chat_model(provider, model_id)
    assert model.anthropic_api_key.get_secret_value() == "isolated-test-key"
    assert model.anthropic_api_url == mm.MODEL_PRESETS[provider]["base_url"]
    assert model.default_headers["x-opencode-session"] == mc.opencode_session_id()


def test_zen_responses_request_keeps_tools_and_uses_responses_api(monkeypatch):
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(mc.settings, "opencode_zen_api_key", "zen-key")
    model = mc.build_chat_model("opencode_zen", "gpt-5.4")
    payload = model._get_request_payload([HumanMessage("hello")])
    assert model.use_responses_api is True
    assert payload["input"]
    assert "messages" not in payload
    assert payload["store"] is False


async def test_auth_console_returns_to_masked_key_entry_and_stores_only_zen(monkeypatch, secrets):
    from textual.app import App
    from textual.widgets import Button, Input

    from novacode_cli.tui.auth_screens import AuthManagerScreen, AuthPromptScreen
    from tests.test_auth_screens import _settle

    browser = Mock(return_value=True)
    monkeypatch.setattr("novacode_cli.tui.provider_signin.webbrowser.open", browser)
    app = App()
    async with app.run_test(size=(110, 40)) as pilot:
        manager = AuthManagerScreen(focus="opencode_zen")
        app.push_screen(manager)
        assert await _settle(pilot, lambda: "opencode_zen" in manager._names)
        manager.open_prompt()
        assert await _settle(pilot, lambda: bool(app.screen.query("#opencode-console")))
        app.screen.query_one("#opencode-console", Button).press()
        assert await _settle(pilot, lambda: isinstance(app.screen, AuthPromptScreen))
        browser.assert_called_once_with("https://opencode.ai/auth")
        key_field = app.screen.query_one(Input)
        assert key_field.password is True
        assert secrets.store == {}
        key_field.value = "zen-private-test-key"
        key_field.focus()
        await pilot.press("enter")
        assert await _settle(
            pilot, lambda: secrets.store.get("opencode_zen_api_key") == "zen-private-test-key"
        )
        assert "opencode_api_key" not in secrets.store


def test_saved_zen_key_is_loaded_and_masked_after_restart(secrets):
    from novacode_cli.config.config import Settings

    secrets.store = {"opencode_zen_api_key": "persisted-secret", "opencode_api_key": "go-secret"}
    loaded = Settings.from_environment()
    assert loaded.opencode_zen_api_key == "persisted-secret"
    assert loaded.opencode_api_key == "go-secret"
    assert "persisted-secret" not in repr(loaded)
    assert "go-secret" not in repr(loaded)


@pytest.mark.parametrize("provider", ["opencode", "opencode_zen"])
def test_public_model_list_is_not_reported_as_authenticated(monkeypatch, provider):
    from novacode_cli.onboarding_check import check_key

    network = Mock(return_value=SimpleNamespace(status_code=200))
    monkeypatch.setattr("novacode_cli.onboarding_check.requests.get", network)
    result = check_key(provider, "invalid-key")
    assert "API key accepted" not in result.message
    network.assert_not_called()


def test_zen_credential_never_reaches_shell_environment():
    from novacode_cli.shell.utils import _sanitize_env

    assert _sanitize_env({"OPENCODE_ZEN_API_KEY": "secret", "PATH": "safe"}) == {"PATH": "safe"}


@pytest.mark.parametrize("model_id", ["jev-1.13", "gemini-3.1-pro"])
def test_unsupported_native_models_fail_before_network(monkeypatch, model_id):
    monkeypatch.setattr(mc.settings, "opencode_zen_api_key", "zen-key")
    assert mc.create_model_from_config("opencode_zen", model_id) is None
