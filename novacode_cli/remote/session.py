"""Restore a session's opted-in remote connections without saving credentials."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from typing import TYPE_CHECKING, Any

from novacode_cli.remote.config import async_load_remote_config

if TYPE_CHECKING:
    from novacode_cli.states.Session import SessionState


def connection_payload(value: object) -> list[dict[str, Any]]:
    """Keep only supported destinations and settings from persisted metadata."""
    connections: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(value, list):
        return []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform")
        chat = entry.get("chat_id")
        if platform not in ("telegram", "discord") or isinstance(chat, bool):
            continue
        if not isinstance(chat, (str, int)):
            continue
        chat = str(chat)
        pattern = r"-?[1-9][0-9]*" if platform == "telegram" else r"[1-9][0-9]*"
        if not re.fullmatch(pattern, chat):
            continue
        item: dict[str, Any] = {"platform": platform, "chat_id": chat}
        credential_id = entry.get("credential_id")
        if credential_id is not None:
            if not isinstance(credential_id, str) or not re.fullmatch(
                r"[a-f0-9]{64}", credential_id
            ):
                continue
            item["credential_id"] = credential_id
        if platform == "discord":
            item["ping"] = entry.get("ping") is not False
        connections[platform, chat] = item
    return list(connections.values())


def remote_connections_payload(state: SessionState) -> list[dict[str, Any]]:
    """Snapshot the session's opt-in, including temporarily unavailable bridges."""
    return connection_payload(getattr(state, "remote_connections", []))


def remember_connection(
    state: SessionState,
    platform: str,
    chat_id: str,
    *,
    ping: bool,
    token: str,
) -> None:
    """Record a successful explicit start for the next session save."""
    state.remote_connections = connection_payload(
        [
            *remote_connections_payload(state),
            {
                "platform": platform,
                "chat_id": chat_id,
                "ping": ping,
                "credential_id": hashlib.sha256(token.encode()).hexdigest(),
            },
        ]
    )


def forget_connection(state: SessionState, bridge_id: str | None = None) -> None:
    """Explicit stop disables reconnection; process shutdown leaves the opt-in alone."""
    state.remote_connections = [
        entry
        for entry in remote_connections_payload(state)
        if bridge_id is not None and f"{entry['platform']}:{entry['chat_id']}" != bridge_id
    ]


async def reconnect_remote(
    state: SessionState,
    *,
    connections: object = None,
) -> list[str]:
    """Reconnect recorded destinations using current credentials and authorization.

    Changed or forgotten credentials require an explicit start. Never create
    a replacement channel or enable auto-approval as part of session recovery.
    Failed connections remain opted in so a later resume can retry them.
    """
    targets = (
        remote_connections_payload(state)
        if connections is None
        else connection_payload(connections)
    )
    if not targets:
        return []
    saved = await async_load_remote_config()
    manager = state._remote_bridge_manager
    if manager is None:
        from novacode_cli.remote.bridge import RemoteBridgeManager

        if state._remote_message_queue is None:
            state._remote_message_queue = asyncio.Queue(maxsize=100)
        manager = state._remote_bridge_manager = RemoteBridgeManager(state._remote_message_queue)
    notices = []
    for entry in targets:
        platform, chat = entry["platform"], entry["chat_id"]
        config = saved.get(platform, {}) if isinstance(saved, dict) else {}
        destination = "channel_id" if platform == "discord" else "chat_id"
        if (
            not isinstance(config, dict)
            or not isinstance(config.get("token"), str)
            or not config["token"]
            or (
                hashlib.sha256(config["token"].encode()).hexdigest() != entry["credential_id"]
                if "credential_id" in entry
                else str(config.get(destination, "")) != chat
            )
        ):
            notices.append(
                f"{platform.title()} reconnect skipped: saved credentials or destination changed. "
                f"Use /remote start {platform}."
            )
            continue
        bridge_id = f"{platform}:{chat}"
        existing = manager._bridges.get(bridge_id)
        if existing and existing.get("task") and not existing["task"].done():
            continue
        try:
            async with asyncio.timeout(30):
                if platform == "discord":
                    success, _error = await manager.start_discord(
                        token=config["token"],
                        channel_id=chat,
                        ping=entry["ping"],
                    )
                else:
                    success, _error = await manager.start_telegram(
                        token=config["token"],
                        chat_id=int(chat),
                    )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await manager.stop_bridge(bridge_id)
            raise
        except Exception:  # noqa: BLE001 — recovery must not prevent local resume.
            success = False
        if not success:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await manager.stop_bridge(bridge_id)
        notices.append(
            f"{platform.title()} remote connection restored."
            if success
            else f"{platform.title()} reconnect failed. Use /remote start {platform} to retry."
        )
    return notices
