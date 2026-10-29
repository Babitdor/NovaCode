"""An auth save refreshes the existing model client without switching models."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.tui.app import NovaApp

if TYPE_CHECKING:
    from novacode_cli.tui.auth_screens import AuthManagerScreen


@pytest.mark.parametrize("provider", ["commandcode", "openai", "google"])
async def test_auth_save_rebuilds_the_live_model(monkeypatch: pytest.MonkeyPatch, provider: str):
    from novacode_cli.config import model_create
    from novacode_cli.config.model_manager import ModelManager

    replacement = object()
    build = Mock(return_value=replacement)
    monkeypatch.setattr(model_create, "create_model_from_config", build)
    monkeypatch.setattr(ModelManager, "get_available_providers", lambda _: [])
    switch = AsyncMock(return_value=("new-agent", "same-workspace-backend"))

    async def prompt(screen: AuthManagerScreen) -> None:
        screen.saved.append(provider)

    host = SimpleNamespace(
        push_screen_wait=prompt,
        _log=Mock(),
        _model_provider=provider,
        model_name="existing-model",
        session_state=SimpleNamespace(switch_model=switch),
        agent="old-agent",
        backend="old-backend",
    )
    await NovaApp._run_auth(host)
    build.assert_called_once_with(provider, "existing-model")
    switch.assert_awaited_once_with(replacement)
    assert host.agent == "new-agent"
    assert host.backend == "same-workspace-backend"
    assert host.model_name == "existing-model"


async def test_auth_cancel_does_not_rebuild(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config import model_create

    build = Mock()
    monkeypatch.setattr(model_create, "create_model_from_config", build)
    host = SimpleNamespace(push_screen_wait=AsyncMock(), _log=Mock())
    await NovaApp._run_auth(host)
    build.assert_not_called()


async def test_auth_refresh_failure_preserves_agent_and_hides_sdk_details(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.config import model_create
    from novacode_cli.config.model_manager import ModelManager

    monkeypatch.setattr(
        model_create,
        "create_model_from_config",
        Mock(side_effect=ValueError("private-test-credential")),
    )
    monkeypatch.setattr(ModelManager, "get_available_providers", lambda _: [])

    async def prompt(screen: AuthManagerScreen) -> None:
        screen.saved.append("commandcode")

    host = SimpleNamespace(
        push_screen_wait=prompt,
        _log=Mock(),
        _model_provider="commandcode",
        model_name="same",
        session_state=SimpleNamespace(switch_model=AsyncMock()),
        agent="old",
        backend="old",
    )
    await NovaApp._run_auth(host)
    assert host.agent == "old"
    host.session_state.switch_model.assert_not_called()
    assert "private-test-credential" not in str(host._log.call_args_list)


async def test_other_provider_auth_rebuilds_delegates_without_changing_live_model(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.config.model_manager import ModelManager

    monkeypatch.setattr(ModelManager, "get_available_providers", lambda _: [])
    model = object()

    async def prompt(screen: AuthManagerScreen) -> None:
        screen.saved.append("google")

    switch = AsyncMock(return_value=("fresh", "backend"))
    host = SimpleNamespace(
        push_screen_wait=prompt,
        _log=Mock(),
        _model_provider="commandcode",
        model_name="same",
        session_state=SimpleNamespace(switch_model=switch, _model=model),
        agent="old",
        backend="old",
    )
    await NovaApp._run_auth(host)
    switch.assert_awaited_once_with(model)
