"""The Decisions tab changes pruning settings independently of chat models."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.app import App
from textual.widgets import Button, Input, Select, Static, Switch, Tabs

from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.decision_settings import DecisionSettings
from novacode_cli.tui.screens import ModelScreen

if TYPE_CHECKING:
    from pathlib import Path

    from textual.pilot import Pilot


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.config.config as config_mod

    class SecretManager:
        def get_secret(self, _name: str) -> None:
            return None

    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", SecretManager)
    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.config.credentials.credential_value", lambda _var: "")
    monkeypatch.setattr(ModelScreen, "_load", lambda self: self._apply({}, {}, []))


async def open_decisions(app: App, pilot: Pilot):
    screen = ModelScreen("ollama", "chat-model")
    app.push_screen(screen)
    await pilot.pause()
    screen.query_one("#model-tabs", Tabs).active = "tab-decisions"
    panel = screen.query_one(DecisionSettings)
    for _ in range(120):
        await pilot.pause()
        if panel._loaded:
            return screen, panel
    message = "Decisions tab did not load"
    raise AssertionError(message)


async def save_and_settle(screen: ModelScreen, pilot: Pilot):
    screen._submit()
    for _ in range(120):
        await pilot.pause()
        if screen.app.screen is not screen:
            return
    message = "Decision settings were not saved"
    raise AssertionError(message)


async def test_decisions_tab_is_separate_and_cancel_does_not_save():
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        assert panel.display is True
        assert screen.query_one("#model-options").display is False
        assert screen.query_one("#model-custom-row").display is False
        assert str(screen.query_one("#switch", Button).label) == "Save"
        panel.query_one("#decision-enabled", Switch).value = True
        await pilot.press("escape")
        assert NovaConfig().get_tool_verdicts_enabled() is False


async def test_openai_preset_saved_without_changing_chat_model(monkeypatch):
    monkeypatch.setattr(
        "novacode_cli.config.credentials.credential_value",
        lambda name: "dummy" if name == "OPENAI_API_KEY" else "",
    )
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-preset", Select).value = "openai"
        await pilot.pause()
        assert panel.query_one("#decision-model", Input).value == "gpt-6-luna"
        assert (
            panel.query_one("#decision-endpoint", Input).value
            == NovaConfig.TOOL_VERDICT_OPENAI_ENDPOINT
        )
        panel.query_one("#decision-enabled", Switch).value = True
        await save_and_settle(screen, pilot)
        assert NovaConfig().get_tool_verdict_endpoint() == NovaConfig.TOOL_VERDICT_OPENAI_ENDPOINT
        assert NovaConfig().get_model_config() is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_toggle_and_future_custom_model_are_persisted(enabled: bool):  # noqa: FBT001
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-enabled", Switch).value = enabled
        panel.query_one("#decision-preset", Select).value = "custom"
        await pilot.pause()
        panel.query_one("#decision-model", Input).value = "kev1:future"
        panel.query_one("#decision-endpoint", Input).value = "http://localhost:9999/v1/systemone"
        await save_and_settle(screen, pilot)
        config = NovaConfig()
        assert config.get_tool_verdicts_enabled() is enabled
        assert config.get_tool_verdict_model() == "kev1:future"
        assert config.get_tool_verdict_endpoint() == "http://localhost:9999/v1/systemone"
        assert config.get_model_config() is None


async def test_jev_preset_requires_auth_only_when_enabled():
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-preset", Select).value = "jev"
        await pilot.pause()
        assert panel.query_one("#decision-model", Input).value == "jev-latest"
        assert (
            panel.query_one("#decision-endpoint", Input).value
            == NovaConfig.TOOL_VERDICT_JEV_ENDPOINT
        )
        panel.query_one("#decision-enabled", Switch).value = True
        screen._submit()
        await pilot.pause()
        assert "API key" in str(panel.query_one("#decision-status", Static).content)
        assert screen.is_mounted
        assert NovaConfig().get_tool_verdicts_enabled() is False
        panel.query_one("#decision-enabled", Switch).value = False
        await save_and_settle(screen, pilot)
        assert NovaConfig().get_tool_verdict_model() == "jev-latest"


async def test_authenticated_jev_can_be_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("novacode_cli.config.credentials.credential_value", lambda _var: "test-key")
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-preset", Select).value = "jev"
        panel.query_one("#decision-enabled", Switch).value = True
        await pilot.pause()
        await save_and_settle(screen, pilot)
        config = NovaConfig()
        assert config.get_tool_verdicts_enabled() is True
        assert config.get_tool_verdict_model() == "jev-latest"
        assert "test-key" not in config.config_path.read_text(encoding="utf-8")


async def test_loading_does_not_replace_a_configured_tev1_variant():
    NovaConfig().set_tool_verdict_settings(
        enabled=True, endpoint=NovaConfig.TOOL_VERDICT_DEFAULT_ENDPOINT, model="tev1:0.8b"
    )
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        await pilot.pause()
        assert panel.query_one("#decision-model", Input).value == "tev1:0.8b"
        assert panel.query_one("#decision-enabled", Switch).value is True
        screen.query_one("#model-tabs", Tabs).active = "tab-models"
        await pilot.pause()
        assert panel.display is False
        assert screen.query_one("#model-options").display is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://example.test",
        "https://key:secret@example.test",
        "http://[broken",
        "http://localhost:bad",
    ],
)
async def test_invalid_endpoints_do_not_save(endpoint: str):
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-endpoint", Input).value = endpoint
        screen._submit()
        await pilot.pause()
        assert screen.is_mounted
        assert "Endpoint" in str(panel.query_one("#decision-status", Static).content)
        assert not NovaConfig().config_path.exists()


async def test_failed_config_write_keeps_the_dialog_open(monkeypatch: pytest.MonkeyPatch):
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)

        def fail_save(_self: NovaConfig) -> None:
            message = "test failure"
            raise OSError(message)

        monkeypatch.setattr(NovaConfig, "_save", fail_save)
        screen._submit()
        await pilot.pause()
        assert screen.is_mounted
        assert "Could not save" in str(panel.query_one("#decision-status", Static).content)


async def test_nested_auth_preserves_the_decision_draft():
    from novacode_cli.tui.auth_screens import AuthManagerScreen

    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen, panel = await open_decisions(app, pilot)
        panel.query_one("#decision-model", Input).value = "future-model"
        panel.query_one("#decision-enabled", Switch).value = True
        panel.open_auth()
        for _ in range(120):
            await pilot.pause()
            if isinstance(app.screen, AuthManagerScreen):
                break
        assert isinstance(app.screen, AuthManagerScreen)
        app.screen.dismiss(None)
        await pilot.pause()
        assert app.screen is screen
        assert panel.query_one("#decision-model", Input).value == "future-model"
        assert panel.query_one("#decision-enabled", Switch).value is True


@pytest.mark.parametrize("enabled", [True, False])
async def test_decision_save_rebuilds_agent_without_changing_chat_model(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,  # noqa: FBT001
):
    from types import SimpleNamespace

    from novacode_cli.tui.app import NovaApp

    live_model = object()
    calls = []

    async def rebuild(model: object) -> tuple[str, str]:
        calls.append(model)
        return "new-agent", "new-backend"

    async def pick(_screen: ModelScreen) -> dict[str, str | bool]:
        return {"kind": "decision", "enabled": enabled, "model": "kev1"}

    class ModelManager:
        def get_current_provider_id(self) -> str:
            return "ollama"

    monkeypatch.setattr("novacode_cli.config.model_manager.ModelManager", ModelManager)
    host = SimpleNamespace(
        session_state=SimpleNamespace(_model=live_model, switch_model=rebuild),
        model_name="chat-model",
        push_screen_wait=pick,
        _log=lambda _msg: None,
    )
    await NovaApp._run_model(host)
    assert calls == [live_model]
    assert host.agent == "new-agent"
    assert host.backend == "new-backend"
    assert host.model_name == "chat-model"
