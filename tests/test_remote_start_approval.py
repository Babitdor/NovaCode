"""Starting Telegram or Discord preserves the session's chosen approval policy."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from rich.console import Console

from novacode_cli.commands.commands import _handle_remote_command


@pytest.fixture
def remote_command(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimpleNamespace, SimpleNamespace, Console, StringIO]:
    """Isolate bridge startup and credential persistence from external services."""
    manager = SimpleNamespace(
        start_discord=AsyncMock(return_value=(True, "")),
        start_telegram=AsyncMock(return_value=(True, "")),
        stop_all=AsyncMock(),
        stop_bridge=AsyncMock(),
        get_bridge=Mock(return_value=None),
        active_bridges=[],
        _bridges={},
    )
    state = SimpleNamespace(
        _remote_bridge_manager=manager,
        auto_approve=False,
        _pre_remote_auto_approve=None,
    )
    monkeypatch.setattr(
        "novacode_cli.remote.config.async_load_remote_config",
        AsyncMock(return_value={}),
    )
    for name in ("async_save_discord_config", "async_save_telegram_config"):
        monkeypatch.setattr(f"novacode_cli.remote.config.{name}", AsyncMock())
    output = StringIO()
    return state, manager, Console(file=output, color_system=None), output


@pytest.mark.parametrize("platform", ["telegram", "discord"])
@pytest.mark.parametrize("auto_approve", [False, True])
@pytest.mark.parametrize("previous", [None, False])
async def test_successful_start_preserves_current_and_legacy_approval_settings(
    remote_command: tuple[SimpleNamespace, SimpleNamespace, Console, StringIO],
    *,
    platform: str,
    auto_approve: bool,
    previous: bool | None,
) -> None:
    state, manager, console, output = remote_command
    state.auto_approve = auto_approve
    state._pre_remote_auto_approve = previous
    target = "--chat" if platform == "telegram" else "--channel"

    assert await _handle_remote_command(
        f"start {platform} --token fixture-token {target} 123",
        state,
        console,
    )

    getattr(manager, f"start_{platform}").assert_awaited_once()
    assert "bridge started!" in output.getvalue()
    assert state.auto_approve is auto_approve
    assert state._pre_remote_auto_approve is previous
    assert "Auto-approve enabled" not in output.getvalue()


@pytest.mark.parametrize("command", ["stop", "stop all", "stop telegram:123", "stop discord:123"])
@pytest.mark.parametrize("previous", [False, True])
async def test_stop_restores_legacy_approval_override(
    remote_command: tuple[SimpleNamespace, SimpleNamespace, Console, StringIO],
    *,
    command: str,
    previous: bool,
) -> None:
    state, manager, console, _output = remote_command
    state.auto_approve = not previous
    state._pre_remote_auto_approve = previous

    assert await _handle_remote_command(command, state, console)

    if command in {"stop", "stop all"}:
        manager.stop_all.assert_awaited_once()
    else:
        manager.stop_bridge.assert_awaited_once_with(command.split()[1])
    assert state.auto_approve is previous
    assert state._pre_remote_auto_approve is None


@pytest.mark.parametrize("command", ["stop", "stop telegram:123"])
@pytest.mark.parametrize("auto_approve", [False, True])
async def test_stop_without_legacy_override_preserves_explicit_policy(
    remote_command: tuple[SimpleNamespace, SimpleNamespace, Console, StringIO],
    *,
    command: str,
    auto_approve: bool,
) -> None:
    state, _manager, console, _output = remote_command
    state.auto_approve = auto_approve

    assert await _handle_remote_command(command, state, console)

    assert state.auto_approve is auto_approve
    assert state._pre_remote_auto_approve is None


async def test_stopping_one_of_several_bridges_keeps_legacy_override_until_last_stop(
    remote_command: tuple[SimpleNamespace, SimpleNamespace, Console, StringIO],
) -> None:
    state, manager, console, _output = remote_command
    state.auto_approve = True
    state._pre_remote_auto_approve = False
    manager.active_bridges = [{"id": "discord:456"}]

    assert await _handle_remote_command("stop telegram:123", state, console)
    assert state.auto_approve is True
    assert state._pre_remote_auto_approve is False

    manager.active_bridges = []
    assert await _handle_remote_command("stop discord:456", state, console)
    assert state.auto_approve is False
    assert state._pre_remote_auto_approve is None
