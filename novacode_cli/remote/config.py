"""Persistent configuration for remote bridges.

Stores Discord/Telegram tokens and channel IDs so the user doesn't have
to re-type them every session.  Config is saved to ``~/.nova/remote.json``.

Format::

    {
        "discord": {
            "token": "MTIz...",
            "channel_id": "1506281315832430652"
        },
        "telegram": {
            "token": "123456:ABC...",
            "chat_id": "-1001234567890"
        }
    }

Only the last-used configuration for each platform is stored.
Tokens are stored in plaintext — the file is created with restrictive
permissions (0600 on Unix) to limit access.

All public functions have an ``async_`` variant that offloads synchronous
file I/O to a thread pool via :func:`asyncio.to_thread`, so callers in
async contexts never block the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path.home() / ".nova"
_CONFIG_FILE = _CONFIG_DIR / "remote.json"


# ── Sync helpers (for internal and sync-context use) ────────────────────────


def _ensure_config_dir() -> None:
    """Ensure the ~/.nova directory exists."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_remote_config() -> dict[str, Any]:
    """Load the remote bridge configuration from disk.

    Returns:
        Dict with keys "discord" and/or "telegram", each containing
        "token" and "channel_id"/"chat_id".  Empty dict if no config.
    """
    if not _CONFIG_FILE.exists():
        return {}

    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load remote config: {e}")
        return {}


def save_remote_config(config: dict[str, Any], *, merge: bool = True) -> None:
    """Save the remote bridge configuration to disk.

    By default merges with existing config so only specified platforms
    are updated.  Use merge=False to overwrite completely.
    Use /remote forget to clear config.

    Args:
        config: Dict with platform configs to save.
        merge: If True, merge with existing config. If False, overwrite.
    """
    _ensure_config_dir()

    if merge and config:
        existing = load_remote_config()
        for platform, values in config.items():
            if isinstance(values, dict) and isinstance(existing.get(platform), dict):
                existing[platform] = {**existing[platform], **values}
            else:
                existing[platform] = values
        data_to_write = existing
    else:
        data_to_write = config

    try:
        from novacode_cli.security.secret_files import write_secret_json
        write_secret_json(_CONFIG_FILE, data_to_write)

        try:
            os.chmod(_CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

        logger.info(f"Remote config saved to {_CONFIG_FILE}")
    except OSError as e:
        logger.error(f"Could not save remote config: {e}")


# ── Async wrappers (for async call paths — do not block the event loop) ─────


async def async_load_remote_config() -> dict[str, Any]:
    """Async version of :func:`load_remote_config`.

    Offloads file I/O to a thread pool so the event loop is never blocked.
    """
    return await asyncio.to_thread(load_remote_config)


async def async_save_remote_config(config: dict[str, Any], *, merge: bool = True) -> None:
    """Async version of :func:`save_remote_config`.

    Offloads file I/O to a thread pool so the event loop is never blocked.
    """
    return await asyncio.to_thread(save_remote_config, config, merge=merge)


async def async_get_discord_config() -> dict[str, str] | None:
    """Async version of :func:`get_discord_config`."""
    config = await async_load_remote_config()
    discord_cfg = config.get("discord")
    if discord_cfg and "token" in discord_cfg:
        return discord_cfg
    return None


async def async_get_telegram_config() -> dict[str, str] | None:
    """Async version of :func:`get_telegram_config`."""
    config = await async_load_remote_config()
    telegram_cfg = config.get("telegram")
    if telegram_cfg and "token" in telegram_cfg:
        return telegram_cfg
    return None


async def async_save_discord_config(token: str, channel_id: str, ping: bool = True) -> None:
    """Async version of :func:`save_discord_config`."""
    await async_save_remote_config({"discord": {"token": token, "channel_id": str(channel_id), "ping": ping}})


async def async_save_telegram_config(token: str, chat_id: str | int) -> None:
    """Async version of :func:`save_telegram_config`."""
    await async_save_remote_config({"telegram": {"token": token, "chat_id": str(chat_id)}})


def get_discord_config() -> dict[str, Any] | None:
    """Get the saved Discord configuration.

    Returns:
        Dict with "token", "channel_id", and optionally "ping", or None if not saved.
    """
    config = load_remote_config()
    discord_cfg = config.get("discord")
    if discord_cfg and "token" in discord_cfg:
        return discord_cfg
    return None


def get_telegram_config() -> dict[str, Any] | None:
    """Get the saved Telegram configuration.

    Returns:
        Dict with "token" and "chat_id", or None if not saved.
    """
    config = load_remote_config()
    telegram_cfg = config.get("telegram")
    if telegram_cfg and "token" in telegram_cfg:
        return telegram_cfg
    return None


def save_discord_config(token: str, channel_id: str, ping: bool = True) -> None:
    """Save Discord bridge configuration.

    Args:
        token: Discord bot token.
        channel_id: Discord channel ID.
        ping: Whether to ping/mention the user when the task is done.
    """
    save_remote_config({"discord": {"token": token, "channel_id": str(channel_id), "ping": ping}})


def save_telegram_config(token: str, chat_id: str | int) -> None:
    """Save Telegram bridge configuration.

    Args:
        token: Telegram bot token.
        chat_id: Telegram chat ID.
    """
    save_remote_config({"telegram": {"token": token, "chat_id": str(chat_id)}})
