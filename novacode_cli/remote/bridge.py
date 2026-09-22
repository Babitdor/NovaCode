"""Remote bridge — send messages to the Nova-Code agent from Discord or Telegram.

Architecture
------------
The bridge runs as background ``asyncio.Task``s inside the same event loop as
the CLI.  When a message arrives on Discord or Telegram, it is placed on an
``asyncio.Queue``.  The main input loop (in ``main.py``) checks this queue
each turn and, if a remote message is pending, executes it through
``execute_task()`` just like a local prompt.

Response chunks are captured from the agent's output and sent back to the
originating platform.  For Discord, messages longer than 2 000 characters
are split into chunks.  For Telegram the limit is 4 096.

Security
--------
- Only allowlisted channel / chat IDs receive responses.
- A ``/remote allow <ID>`` command manages the allowlist.
- Un-authorised messages are silently ignored (no acknowledgement leaked).

Lifecycle
---------
- ``/remote start discord  --token TOKEN --channel ID`` starts the bridge.
- ``/remote start telegram --token TOKEN --chat   ID`` starts the bridge.
- ``/remote stop``  gracefully shuts down all bridges.
- ``/remote status`` shows what's running.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class RemotePlatform(str, Enum):
    """Supported remote platforms / message sources.

    DISCORD and TELEGRAM are interactive chat bridges. CRON and WEBHOOK are
    one-way *event sources* (Enhancement 3 / 5): they push ``RemoteMessage``s
    onto the same queue but have no chat to reply to (their ``reply_fn`` is a
    no-op). The processor handles all four identically.
    """
    DISCORD = "discord"
    TELEGRAM = "telegram"
    CRON = "cron"
    WEBHOOK = "webhook"


@dataclass
class RemoteMessage:
    """A message received from a remote platform.

    Attributes:
        platform: Which platform the message came from.
        chat_id: Platform-specific channel/chat ID (str for Discord, int for Telegram).
        user_name: Display name of the sender.
        text: The raw text of the message.
        reply_fn: Async callable that sends a response back to the originating chat.
        typing_fn: Optional async callable that triggers a "typing" indicator on the platform.
        react_fn: Optional async callable that adds a reaction emoji to the user's
            message (Discord). Best-effort; ``None`` on platforms without reactions.
        edit_fn: Optional async callable ``(text, final=False)`` that creates a
            single "live" reply message on first call and edits it in place on
            subsequent calls — used to stream the agent's answer GPT-style without
            flooding the chat or hitting rate limits. ``final=True`` signals the
            last edit (the bridge may then apply richer formatting). ``None`` if
            the platform/bridge doesn't support edit-in-place streaming.
    """
    platform: RemotePlatform
    chat_id: str | int
    user_name: str
    text: str
    reply_fn: Callable[[str], Awaitable[None]]
    typing_fn: Callable[[], Awaitable[None]] | None = None
    react_fn: Callable[[str], Awaitable[None]] | None = None
    edit_fn: Callable[..., Awaitable[None]] | None = None
    user_mention: str | None = None
    #: Forum topic the message was posted in (Telegram), or None.
    thread_id: int | None = None
    #: The session that sent the message this one replies to, if known.
    reply_to_owner: str | None = None
    #: Set by the router before replying: ``{"sid": ..., "label": ...}``. The
    #: bridge reads it when sending, to remember which session each outgoing
    #: message belongs to (so replying to it reaches that session).
    route: dict = field(default_factory=dict)


@dataclass
class BridgeConfig:
    """Configuration for a single bridge instance.

    Attributes:
        platform: Which platform.
        token: Bot token for the platform API.
        chat_id: Specific channel/chat to listen on and respond in.
        allowed_ids: Set of additional IDs that are allowed to interact.
        ping: Whether to ping/mention the user when the task is done (Discord).
    """
    platform: RemotePlatform
    token: str
    chat_id: str | int
    allowed_ids: set[str | int] = field(default_factory=set)
    ping: bool = True


# ---------------------------------------------------------------------------
# Platform limits
# ---------------------------------------------------------------------------

MAX_MESSAGE_LENGTH: dict[RemotePlatform, int] = {
    RemotePlatform.DISCORD: 2000,
    RemotePlatform.TELEGRAM: 4096,
}


def chunk_message(text: str, platform: RemotePlatform) -> list[str]:
    """Split a long response into chunks that fit the platform's message limit.

    Tries to break on paragraph boundaries first, then on sentence
    boundaries, then on word boundaries.  As a last resort, hard-breaks.

    Args:
        text: Full response text.
        platform: Target platform.

    Returns:
        List of message chunks, each <= the platform's limit.
    """
    limit = MAX_MESSAGE_LENGTH.get(platform, 2000)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []

    # First try paragraph breaks
    paragraphs = re.split(r"\n{2,}", text)
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para) if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single paragraph exceeds the limit, split by sentences
            if len(para) > limit:
                chunks.extend(_split_by_sentences(para, limit))
                current = ""
            else:
                current = para
    if current:
        chunks.append(current)

    # Final pass: hard-break any chunks that still exceed the limit
    # (e.g., a string with no spaces, sentences, or paragraphs)
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= limit:
            result.append(chunk)
        else:
            # Hard-break at limit boundaries
            for i in range(0, len(chunk), limit):
                result.append(chunk[i:i + limit])

    return result or [text[:limit]]


def format_tool_digest(tool_names: list[str], *, max_shown: int = 8) -> str:
    """Condense a turn's tool activity into a single chat-friendly line.

    Remote chats were being flooded with one message per tool call. Instead,
    callers accumulate the tool names used during a turn and send the result of
    this function ONCE, e.g.::

        🔧 12 tool calls · `read_file×4, grep×3, shell×2, write_file×2, task`

    The tool list is wrapped in backticks so Telegram's ``parse_mode=Markdown``
    treats underscores in names like ``read_file`` literally instead of as
    italics (which would otherwise mangle or reject the message).

    Args:
        tool_names: Tool names in call order (duplicates expected; they're counted).
        max_shown: Cap on distinct names listed before collapsing to "+N more".

    Returns:
        A single-line summary, or "" if no tools were used.
    """
    names = [n for n in tool_names if n]
    if not names:
        return ""

    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1  # insertion order = first-seen order

    distinct = list(counts.items())
    shown = distinct[:max_shown]
    parts = [f"{name}×{c}" if c > 1 else name for name, c in shown]
    body = ", ".join(parts)
    extra = len(distinct) - len(shown)
    if extra > 0:
        body += f", +{extra} more"

    total = len(names)
    plural = "s" if total != 1 else ""
    return f"🔧 {total} tool call{plural} · `{body}`"


# Tool name -> coarse activity category, for the compact end-of-turn footer.
_TOOL_CATEGORY: dict[str, str] = {
    "read_file": "read", "read": "read", "view": "read", "cat": "read",
    "write_file": "edit", "edit_file": "edit", "write": "edit", "edit": "edit",
    "str_replace": "edit",
    "execute": "run", "run_tests": "run", "shell": "run", "bash": "run",
    "run_command": "run",
    "grep": "search", "glob": "search", "ls": "search", "code_search": "search",
    "find_related_code": "search",
    "web_search": "web", "duckduckgo_search": "web", "fetch_url": "web",
    "docs_search": "web", "package_info": "web",
    "task": "subagent", "think": "think",
}


def categorize_tools(tool_names: list[str]) -> str:
    """Compact category counts for a turn's tools, e.g. ``read×4, edit×2, run×3``.

    Collapses tools into coarse categories so progress reads as *what kind* of
    work is happening, not a list of every tool. Returns "" when none.
    """
    names = [n for n in tool_names if n]
    if not names:
        return ""
    counts: dict[str, int] = {}
    for n in names:
        cat = _TOOL_CATEGORY.get(n, "other")
        counts[cat] = counts.get(cat, 0) + 1
    return ", ".join(f"{cat}×{c}" if c > 1 else cat for cat, c in counts.items())


def format_activity_footer(tool_names: list[str]) -> str:
    """One-line ``-#`` small-text footer, e.g. ``-# 🔧 11 tools · read×4, edit×2``."""
    cats = categorize_tools(tool_names)
    if not cats:
        return ""
    total = len([n for n in tool_names if n])
    plural = "s" if total != 1 else ""
    return f"-# 🔧 {total} tool{plural} · {cats}"


def _split_by_sentences(text: str, limit: int) -> list[str]:
    """Split text into chunks on sentence boundaries."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for s in sentences:
        candidate = (current + " " + s) if current else s
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single sentence exceeds limit, hard-split by words
            if len(s) > limit:
                words = s.split()
                for w in words:
                    candidate2 = (current + " " + w) if current else w
                    if len(candidate2) <= limit:
                        current = candidate2
                    else:
                        if current:
                            chunks.append(current)
                        current = w
            else:
                current = s
    if current:
        chunks.append(current)

    # Hard-break any oversized chunks
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= limit:
            result.append(chunk)
        else:
            for i in range(0, len(chunk), limit):
                result.append(chunk[i:i + limit])

    return result or [text[:limit]]


# ---------------------------------------------------------------------------
# Bridge manager (singleton)
# ---------------------------------------------------------------------------


class RemoteBridgeManager:
    """Manages active remote bridge instances.

    Holds a reference to the message queue that the main CLI loop reads,
    and manages the lifecycle of Discord/Telegram bot tasks.

    Each bridge is stored as a dict with keys:
        - "task": The asyncio.Task running the bridge
        - "bridge": The bridge object (DiscordBridge, TelegramBridge, etc.)
        - "config": The BridgeConfig
    """

    _WATCHDOG_INTERVAL = 30  # seconds between liveness checks
    _MAX_RESTARTS = 5        # give up after this many consecutive failures per bridge

    def __init__(self, message_queue: asyncio.Queue[RemoteMessage]) -> None:
        self._queue = message_queue
        self._bridges: dict[str, dict[str, Any]] = {}  # bridge_id → {task, bridge, config}
        self._running = False
        self._on_status: Callable[[str], Awaitable[None]] | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._restart_counts: dict[str, int] = {}  # bridge_id → consecutive restart count

    def set_status_callback(self, callback: Callable[[str], Awaitable[None]] | None) -> None:
        """Set a callback for status messages from bridges.

        The callback receives a status string and can display it to the user
        (e.g., via the Rich console).
        """
        self._on_status = callback

    async def _emit_status(self, msg: str) -> None:
        """Emit a status message via callback."""
        if self._on_status:
            try:
                await self._on_status(msg)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_bridges(self) -> list[dict[str, Any]]:
        """Return info about active bridges for display."""
        info = []
        for bid, entry in self._bridges.items():
            task = entry.get("task")
            bridge = entry.get("bridge")
            config = entry.get("config")
            is_alive = task is not None and not task.done()
            status = "running" if is_alive else "stopped"

            # Check bridge-specific diagnostics
            last_error = None
            bot_user = None
            connected = False
            if bridge is not None:
                last_error = getattr(bridge, "last_error", None)
                bot_user = getattr(bridge, "bot_user", None)
                connected = getattr(bridge, "is_connected", False)

            if not is_alive and last_error:
                status = f"error: {last_error}"
            elif is_alive and not connected:
                status = "connecting..."

            entry_info = {
                "id": bid,
                "platform": config.platform.value,
                "chat_id": config.chat_id,
                "status": status,
                "bot_user": bot_user,
            }
            info.append(entry_info)
        return info

    def bridges(self, platform: RemotePlatform | None = None) -> list[Any]:
        """Live bridge objects, optionally of one platform."""
        return [
            e["bridge"]
            for e in self._bridges.values()
            if e.get("bridge") is not None
            and (platform is None or e["config"].platform == platform)
            and e.get("task") is not None
            and not e["task"].done()
        ]

    def _make_bridge_id(self, platform: RemotePlatform, chat_id: str | int) -> str:
        return f"{platform.value}:{chat_id}"

    async def start_discord(self, token: str, channel_id: str | int, ping: bool = True) -> tuple[bool, str]:
        """Start a Discord bridge.

        Args:
            token: Discord bot token.
            channel_id: Discord channel ID to listen on.
            ping: Whether to ping/mention the user when the task is done.

        Returns:
            (success, error_message) tuple. error_message is empty on success.
        """
        bridge_id = self._make_bridge_id(RemotePlatform.DISCORD, channel_id)

        if bridge_id in self._bridges:
            existing_task = self._bridges[bridge_id].get("task")
            if existing_task and not existing_task.done():
                logger.warning(f"Discord bridge {bridge_id} already running")
                return False, f"Bridge {bridge_id} is already running"

        config = BridgeConfig(
            platform=RemotePlatform.DISCORD,
            token=token,
            chat_id=str(channel_id),
            allowed_ids={str(channel_id)},
            ping=ping,
        )

        # Validate Discord token format (bot tokens: 3 dot-separated segments, ~70+ chars)
        if token.count(".") != 2 or len(token) < 50:
            return False, (
                "Invalid Discord token format. "
                "Bot tokens should be 3 dot-separated segments (e.g., NTIzNjU...). "
                "Get one from https://discord.com/developers/applications"
            )

        try:
            import discord  # noqa: F401 -- eager check for availability

            from novacode_cli.remote.discord_bridge import DiscordBridge

            bridge = DiscordBridge(config=config, message_queue=self._queue, on_status=self._on_status)
            task = asyncio.create_task(bridge.run(), name=f"discord-bridge-{bridge_id}")
            self._bridges[bridge_id] = {
                "task": task,
                "bridge": bridge,
                "config": config,
            }
            self._running = True
            logger.info(f"Discord bridge task created: {bridge_id}")

            # Wait for the gateway connection (up to 15 seconds)
            connected = await bridge.wait_for_ready(timeout=15.0)
            if connected:
                logger.info(f"Discord bridge connected: {bridge_id} as {bridge.bot_user}")
                logger.info(f"Bridge queue size: {self._queue.qsize()}")
                self._ensure_watchdog()
                return True, ""
            else:
                # Task started but not connected yet — check for immediate errors
                if task.done():
                    exc = task.exception()
                    if exc:
                        error_msg = f"Discord bridge failed: {exc}"
                        logger.error(error_msg)
                        del self._bridges[bridge_id]
                        self._running = bool(self._bridges)
                        return False, bridge.last_error or error_msg
                # Still connecting — report success but note pending connection
                logger.info(f"Discord bridge still connecting: {bridge_id}")
                self._ensure_watchdog()
                return True, ""

        except ImportError:
            logger.error("discord.py is not installed. Install it with: pip install discord.py")
            return False, "discord.py is not installed. Install with: pip install discord.py"
        except Exception as e:
            logger.error(f"Failed to start Discord bridge: {e}")
            return False, f"Failed to start Discord bridge: {e}"

    async def start_telegram(self, token: str, chat_id: str | int) -> tuple[bool, str]:
        """Start a Telegram bridge.

        Token is verified against the Telegram API before the background
        task is started.  Invalid tokens return an error immediately.

        Args:
            token: Telegram bot token.
            chat_id: Telegram chat ID to listen on.

        Returns:
            (success, error_message) tuple. error_message is empty on success.
        """
        bridge_id = self._make_bridge_id(RemotePlatform.TELEGRAM, chat_id)

        if bridge_id in self._bridges:
            existing_task = self._bridges[bridge_id].get("task")
            if existing_task and not existing_task.done():
                logger.warning(f"Telegram bridge {bridge_id} already running")
                return False, f"Bridge {bridge_id} is already running"

        config = BridgeConfig(
            platform=RemotePlatform.TELEGRAM,
            token=token,
            chat_id=int(chat_id),
            allowed_ids={int(chat_id)},
        )

        try:
            import aiohttp

            from novacode_cli.remote.telegram_bridge import TelegramBridge

            # Verify the token works before creating the background task
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{token}/getMe"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        data = await resp.json()
                        if not data.get("ok"):
                            error_desc = data.get("description", "Unknown error")
                            return False, f"Telegram API error: {error_desc}"
                        bot_info = data.get("result", {})
                        bot_name = bot_info.get("username", "unknown")
                except aiohttp.ClientError as e:
                    return False, f"Failed to connect to Telegram API: {e}"

            bridge = TelegramBridge(config=config, message_queue=self._queue)
            task = asyncio.create_task(bridge.run(), name=f"telegram-bridge-{bridge_id}")
            self._bridges[bridge_id] = {
                "task": task,
                "bridge": bridge,
                "config": config,
            }
            self._running = True
            logger.info(f"Telegram bridge started: {bridge_id} (@{bot_name})")
            self._ensure_watchdog()
            return True, ""
        except ImportError as e:
            logger.error(f"Missing dependency for Telegram bridge: {e}")
            return False, f"Missing dependency: {e}"
        except Exception as e:
            logger.error(f"Failed to start Telegram bridge: {e}")
            return False, f"Failed to start Telegram bridge: {e}"

    async def start_discord_auto_channel(
        self, token: str, channel_name: str, guild_id: int | None = None, ping: bool = True
    ) -> tuple[bool, str]:
        """Start a Discord bridge and auto-create a channel.

        Connects to Discord, waits for the gateway, creates a text channel
        in the first guild, and starts listening on it.

        Args:
            token: Discord bot token.
            channel_name: Name for the auto-created channel.
            guild_id: Optional specific guild ID. If None, uses first guild.
            ping: Whether to ping/mention the user when the task is done.

        Returns:
            (success, error_message) tuple. On success, the channel ID is
            automatically saved to config.
        """
        # Step 1: Start the bridge without a channel spec
        # We use a placeholder channel ID that we'll update after creating the channel
        temp_channel_id = "0"  # placeholder

        bridge_id = self._make_bridge_id(RemotePlatform.DISCORD, temp_channel_id)

        # Validate token format
        if token.count(".") != 2 or len(token) < 50:
            return False, (
                "Invalid Discord token format. "
                "Bot tokens should be 3 dot-separated segments (e.g., NTIzNjU...). "
                "Get one from https://discord.com/developers/applications"
            )

        try:
            import discord  # noqa: F401

            from novacode_cli.remote.discord_bridge import DiscordBridge

            # Start with placeholder — we'll update after channel creation
            config = BridgeConfig(
                platform=RemotePlatform.DISCORD,
                token=token,
                chat_id=temp_channel_id,
                allowed_ids={temp_channel_id},
                ping=ping,
            )

            bridge = DiscordBridge(
                config=config,
                message_queue=self._queue,
                on_status=self._on_status,
            )
            task = asyncio.create_task(bridge.run(), name=f"discord-bridge-auto")

            # Wait for connection
            connected = await bridge.wait_for_ready(timeout=15.0)
            if not connected:
                # Check for errors
                if task.done() and task.exception():
                    error_msg = str(task.exception())
                elif bridge.last_error:
                    error_msg = bridge.last_error
                else:
                    error_msg = "Timed out waiting for Discord connection"
                if not task.done():
                    task.cancel()
                return False, error_msg

            # Step 2: Create the channel
            success, channel_id, error_msg = await bridge.create_channel(
                channel_name, guild_id=guild_id
            )
            if not success:
                # Stop the bridge since we can't proceed
                if not task.done():
                    task.cancel()
                return False, error_msg

            # Step 3: Update the config with the real channel ID
            config.chat_id = channel_id
            config.allowed_ids = {channel_id}
            bridge._config = config  # update the bridge's config too

            # Remove the placeholder and store with real ID
            if bridge_id in self._bridges:
                del self._bridges[bridge_id]

            real_bridge_id = self._make_bridge_id(RemotePlatform.DISCORD, channel_id)
            self._bridges[real_bridge_id] = {
                "task": task,
                "bridge": bridge,
                "config": config,
            }
            self._running = True

            logger.info(f"Discord bridge started with auto-created channel: {channel_id}")
            return True, f"Channel #{channel_name} created (ID: {channel_id})"

        except ImportError:
            return False, "discord.py is not installed. Install with: pip install discord.py"
        except Exception as e:
            logger.error(f"Failed to start Discord bridge with auto channel: {e}")
            return False, f"Failed to start Discord bridge: {e}"

    async def stop_bridge(self, bridge_id: str | None = None) -> None:
        """Stop a specific bridge or all bridges.

        Args:
            bridge_id: If given, stop this specific bridge. Otherwise stop all.
        """
        if bridge_id:
            entry = self._bridges.pop(bridge_id, None)
            if entry:
                task = entry.get("task")
                bridge = entry.get("bridge")
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if bridge and hasattr(bridge, "stop"):
                    await bridge.stop()
        else:
            for bid, entry in list(self._bridges.items()):
                task = entry.get("task")
                bridge = entry.get("bridge")
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if bridge and hasattr(bridge, "stop"):
                    await bridge.stop()
            self._bridges.clear()
        self._running = any(
            entry.get("task") and not entry["task"].done()
            for entry in self._bridges.values()
        )
        if not self._running and self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

    async def _watchdog(self) -> None:
        """Periodically check that bridge tasks are alive and restart crashed ones.

        Runs as a long-lived asyncio Task while at least one bridge is registered.
        Respects ``_MAX_RESTARTS`` so permanently broken bridges don't loop forever.
        """
        try:
            while True:
                await asyncio.sleep(self._WATCHDOG_INTERVAL)

                for bridge_id, entry in list(self._bridges.items()):
                    task: asyncio.Task | None = entry.get("task")
                    config: BridgeConfig | None = entry.get("config")
                    if task is None or config is None:
                        continue
                    if not task.done():
                        # Still alive — reset the failure counter.
                        self._restart_counts.pop(bridge_id, None)
                        continue

                    count = self._restart_counts.get(bridge_id, 0)
                    if count >= self._MAX_RESTARTS:
                        logger.warning(
                            "Bridge %s exceeded restart limit (%d) — giving up",
                            bridge_id, self._MAX_RESTARTS,
                        )
                        continue

                    logger.warning("Bridge %s died, restarting (attempt %d)", bridge_id, count + 1)
                    self._restart_counts[bridge_id] = count + 1

                    try:
                        if config.platform == RemotePlatform.DISCORD:
                            from novacode_cli.remote.discord_bridge import DiscordBridge 
                            bridge = DiscordBridge(
                                config=config,
                                message_queue=self._queue,
                                on_status=self._on_status,
                            )
                            new_task = asyncio.create_task(
                                bridge.run(), name=f"discord-bridge-{bridge_id}"
                            )
                        else:
                            from novacode_cli.remote.telegram_bridge import TelegramBridge
                            bridge = TelegramBridge(config=config, message_queue=self._queue)
                            new_task = asyncio.create_task(
                                bridge.run(), name=f"telegram-bridge-{bridge_id}"
                            )

                        entry["bridge"] = bridge
                        entry["task"] = new_task
                        await self._emit_status(
                            f"🔄 Restarted {config.platform.value} bridge "
                            f"(attempt {count + 1}/{self._MAX_RESTARTS})"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed to restart bridge %s: %s", bridge_id, exc)

        except asyncio.CancelledError:
            pass

    def _ensure_watchdog(self) -> None:
        """Start the watchdog task if it isn't running."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog(), name="bridge-watchdog")

    async def stop_all(self) -> None:
        """Stop all bridges gracefully."""
        await self.stop_bridge(None)

    def get_bridge(self, bridge_id: str) -> dict[str, Any] | None:
        """Get info about a specific bridge."""
        if bridge_id in self._bridges:
            entry = self._bridges[bridge_id]
            task = entry.get("task")
            bridge = entry.get("bridge")
            config = entry.get("config")
            return {
                "id": bridge_id,
                "config": config,
                "status": "running" if task and not task.done() else "stopped",
                "bot_user": getattr(bridge, "bot_user", None) if bridge else None,
                "last_error": getattr(bridge, "last_error", None) if bridge else None,
            }
        return None
