"""Session reconnect opt-ins survive exit without restoring stale permissions."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage

from novacode_cli.commands.commands import _handle_remote_command
from novacode_cli.remote.session import (
    connection_payload,
    reconnect_remote,
    remember_connection,
    remote_connections_payload,
)
from novacode_cli.session.session_persistence import SessionManager, SessionMeta
from novacode_cli.states.Session import SessionState
from tests.test_remote_start_approval import remote_command  # noqa: F401

if TYPE_CHECKING:
    from io import StringIO
    from pathlib import Path
    from types import SimpleNamespace

    from rich.console import Console


@pytest.fixture
def reconnect_state(monkeypatch: pytest.MonkeyPatch) -> SessionState:
    """Use current isolated credentials and a fake connection manager."""
    from types import SimpleNamespace

    state = SessionState()
    state.remote_connections = [
        {"platform": "telegram", "chat_id": "-123"},
        {"platform": "discord", "chat_id": "456", "ping": False},
    ]
    state._remote_bridge_manager = SimpleNamespace(
        _bridges={},
        start_telegram=AsyncMock(return_value=(True, "")),
        start_discord=AsyncMock(return_value=(True, "")),
        stop_bridge=AsyncMock(),
    )
    monkeypatch.setattr(
        "novacode_cli.remote.session.async_load_remote_config",
        AsyncMock(
            return_value={
                "telegram": {"token": "current-telegram-token", "chat_id": "-123"},
                "discord": {"token": "current-discord-token", "channel_id": "456"},
            }
        ),
    )
    return state


async def test_reconnect_restores_exact_targets_and_preserves_approval_policy(
    reconnect_state: SessionState,
) -> None:
    state = reconnect_state
    before = remote_connections_payload(state)
    notices = await reconnect_remote(state)
    state._remote_bridge_manager.start_telegram.assert_awaited_once_with(
        token="current-telegram-token",  # noqa: S106 — isolated fake credential.
        chat_id=-123,
    )
    state._remote_bridge_manager.start_discord.assert_awaited_once_with(
        token="current-discord-token",  # noqa: S106 — isolated fake credential.
        channel_id="456",
        ping=False,
    )
    assert len(notices) == 2
    assert all("restored" in notice for notice in notices)
    assert not state.auto_approve
    assert remote_connections_payload(state) == before


@pytest.mark.parametrize(
    "config",
    [
        {},
        None,
        [],
        {"telegram": None},
        {
            "telegram": {"token": "rotated", "chat_id": "-999"},
            "discord": {"token": "rotated", "channel_id": "999"},
        },
    ],
)
async def test_missing_or_reconfigured_credentials_do_not_redirect_or_reconnect(
    reconnect_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
    config: object,
) -> None:
    monkeypatch.setattr(
        "novacode_cli.remote.session.async_load_remote_config",
        AsyncMock(return_value=config),
    )
    notices = await reconnect_remote(reconnect_state)
    reconnect_state._remote_bridge_manager.start_telegram.assert_not_awaited()
    reconnect_state._remote_bridge_manager.start_discord.assert_not_awaited()
    assert all("skipped" in notice for notice in notices)


@pytest.mark.parametrize(
    "failure", [RuntimeError("secret-token"), TimeoutError(), (False, "secret-token")]
)
async def test_failed_connection_does_not_block_other_platform_or_lose_opt_in(
    reconnect_state: SessionState,
    failure: object,
) -> None:
    manager = reconnect_state._remote_bridge_manager
    if isinstance(failure, Exception):
        manager.start_telegram.side_effect = failure
    else:
        manager.start_telegram.return_value = failure
    before = remote_connections_payload(reconnect_state)
    notices = await reconnect_remote(reconnect_state)
    manager.stop_bridge.assert_awaited_once_with("telegram:-123")
    manager.start_discord.assert_awaited_once()
    assert "failed" in notices[0]
    assert "secret-token" not in str(notices)
    assert remote_connections_payload(reconnect_state) == before


async def test_cancellation_stops_partially_started_bridge(reconnect_state: SessionState) -> None:
    manager = reconnect_state._remote_bridge_manager
    manager.start_telegram.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await reconnect_remote(reconnect_state)
    manager.stop_bridge.assert_awaited_once_with("telegram:-123")
    manager.start_discord.assert_not_awaited()


async def test_already_running_bridge_is_reused(reconnect_state: SessionState) -> None:
    from unittest.mock import Mock

    manager = reconnect_state._remote_bridge_manager
    task = Mock()
    task.done.return_value = False
    manager._bridges["telegram:-123"] = {"task": task}
    await reconnect_remote(reconnect_state)
    manager.start_telegram.assert_not_awaited()
    manager.start_discord.assert_awaited_once()


async def test_legacy_and_new_sessions_do_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    load = AsyncMock()
    monkeypatch.setattr("novacode_cli.remote.session.async_load_remote_config", load)
    assert await reconnect_remote(SessionState()) == []
    load.assert_not_awaited()


async def test_reconnect_uses_session_destination_when_last_used_chat_changed(
    reconnect_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = reconnect_state
    state.remote_connections = []
    token = "fixture-bot-token"  # noqa: S105 — isolated fake credential.
    remember_connection(state, "telegram", "-123", ping=False, token=token)
    monkeypatch.setattr(
        "novacode_cli.remote.session.async_load_remote_config",
        AsyncMock(return_value={"telegram": {"token": token, "chat_id": "-999"}}),
    )
    await reconnect_remote(state)
    state._remote_bridge_manager.start_telegram.assert_awaited_once_with(token=token, chat_id=-123)
    assert token not in json.dumps(remote_connections_payload(state))


async def test_different_bot_credentials_cannot_restore_a_sessions_destination(
    reconnect_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = reconnect_state
    state.remote_connections = []
    token = "fixture-original-token"  # noqa: S105 — isolated fake credential.
    remember_connection(state, "telegram", "-123", ping=False, token=token)
    monkeypatch.setattr(
        "novacode_cli.remote.session.async_load_remote_config",
        AsyncMock(return_value={"telegram": {"token": "replacement-bot", "chat_id": "-123"}}),
    )
    notices = await reconnect_remote(state)
    state._remote_bridge_manager.start_telegram.assert_not_awaited()
    assert "skipped" in notices[0]


async def test_cleanup_failure_is_nonfatal(reconnect_state: SessionState) -> None:
    manager = reconnect_state._remote_bridge_manager
    manager.start_telegram.return_value = (False, "unavailable")
    manager.stop_bridge.side_effect = RuntimeError("already closed")
    notices = await reconnect_remote(reconnect_state)
    assert "failed" in notices[0]
    manager.start_discord.assert_awaited_once()


async def test_child_reconnect_reuses_root_owned_manager(reconnect_state: SessionState) -> None:
    child = SessionState()
    child.remote_connections = remote_connections_payload(reconnect_state)
    root_manager = reconnect_state._remote_bridge_manager
    reconnect_state.remote_connections = []
    await reconnect_remote(reconnect_state, connections=child.remote_connections)
    root_manager.start_telegram.assert_awaited_once()
    root_manager.start_discord.assert_awaited_once()
    assert child._remote_bridge_manager is None
    assert reconnect_state.remote_connections == []


async def test_tui_resume_restores_the_sessions_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_tui_resume_model import _SS, _drive, _Meta

    meta = _Meta(None, None)
    meta.remote_connections = [{"platform": "telegram", "chat_id": "-123"}]
    calls = []

    async def reconnect(owner: object, *, connections: object = None) -> list[str]:
        calls.append((owner, connections))
        return ["Telegram remote connection restored."]

    monkeypatch.setattr("novacode_cli.remote.session.reconnect_remote", reconnect)
    state = _SS()
    app, _model, logged = await _drive(monkeypatch, state, meta, (None, None))
    assert state.remote_connections == meta.remote_connections
    assert calls == [(state, meta.remote_connections)]
    assert any("connection restored" in notice for notice in logged)
    assert app.session_state.session_id == "resumed-session"


def test_metadata_roundtrip_and_legacy_defaults(
    tmp_path: Path, reconnect_state: SessionState
) -> None:
    manager = SessionManager(tmp_path)
    payload = remote_connections_payload(reconnect_state)
    manager.save_session(
        "session",
        "thread",
        [HumanMessage("Keep this conversation")],
        "nova",
        remote_connections=payload,
    )
    data = manager.load_session("session")
    assert data is not None
    assert data.meta.remote_connections == payload
    serialized = json.dumps(data.meta.to_dict())
    assert "token" not in serialized
    legacy = data.meta.to_dict()
    del legacy["remote_connections"]
    assert SessionMeta.from_dict(legacy).remote_connections == []


def test_metadata_filters_malformed_entries_and_credentials() -> None:
    assert connection_payload(
        [
            {"platform": "telegram", "chat_id": "-123", "token": "never-save"},
            {"platform": "telegram", "chat_id": -123},
            {"platform": "discord", "chat_id": "456", "allowed_user_ids": [123]},
            {"platform": "rootapp", "chat_id": "456"},
            {"platform": "discord", "chat_id": "456 --token evil"},
            {"platform": "telegram", "chat_id": True},
            None,
        ]
    ) == [
        {"platform": "telegram", "chat_id": "-123"},
        {"platform": "discord", "chat_id": "456", "ping": True},
    ]


async def test_explicit_start_stop_and_shutdown_opt_in(
    remote_command: tuple[SimpleNamespace, SimpleNamespace, Console, StringIO],  # noqa: F811
) -> None:
    state, manager, console, _output = remote_command
    await _handle_remote_command("start telegram --token fixture --chat 123", state, console)
    await _handle_remote_command(
        "start discord --token fixture --channel 456 --no-ping", state, console
    )
    assert len(remote_connections_payload(state)) == 2
    discord_connection = remote_connections_payload(state)[1]
    # Shutdown calls manager.stop_all directly, rather than the explicit command.
    await manager.stop_all()
    assert len(remote_connections_payload(state)) == 2
    await _handle_remote_command("stop telegram:123", state, console)
    assert remote_connections_payload(state) == [discord_connection]
    await _handle_remote_command("stop", state, console)
    assert remote_connections_payload(state) == []


async def test_failed_explicit_start_does_not_enable_reconnection(
    remote_command: tuple[SimpleNamespace, SimpleNamespace, Console, StringIO],  # noqa: F811
) -> None:
    state, manager, console, _output = remote_command
    manager.start_telegram.return_value = (False, "unavailable")
    await _handle_remote_command("start telegram --token fixture --chat 123", state, console)
    assert remote_connections_payload(state) == []
