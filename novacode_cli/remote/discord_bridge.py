"""Discord bridge — forwards messages between Discord and the Nova-Code agent.

Uses ``discord.py`` (pip install discord.py) to connect as a bot.
Only messages in the allowlisted channel are processed; all others are
silently ignored.

Message Flow
------------
1. User sends a message in the configured Discord channel.
2. ``DiscordBridge._on_message`` creates a ``RemoteMessage`` and puts it
   on the shared ``asyncio.Queue``.
3. The CLI's main loop reads from the queue and runs ``execute_task()``.
4. The ``reply_fn`` attached to the ``RemoteMessage`` sends the agent's
   response back to the same Discord channel (split into chunks if needed).

Required Setup
--------------
1. Create a Discord Application at https://discord.com/developers/applications
2. Add a Bot user, enable **Message Content Intent** under Privileged Intents
3. Generate a Bot Token
4. Invite the bot to your server with appropriate permissions
5. Use ``/remote start discord --token TOKEN --channel CHANNEL_ID``
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

from novacode_cli.remote.bridge import (
    BridgeConfig,
    RemoteMessage,
    RemotePlatform,
)
from novacode_cli.remote.processor import _debug_log

logger = logging.getLogger(__name__)


class DiscordBridge:
    """Discord bot bridge for Nova-Code.

    Connects to Discord via the gateway and listens for messages in
    the configured channel.  Forwards them to the agent and returns
    responses.
    """

    def __init__(
        self,
        config: BridgeConfig,
        message_queue: asyncio.Queue[RemoteMessage],
        *,
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._queue = message_queue
        self._on_status = on_status  # async callback for status messages
        self._client: Any = None  # discord.Client, lazily created
        self._ready_event = asyncio.Event()
        self._connected = False
        self._last_error: str | None = None
        self._bot_user: str | None = None
        self._msg_count: int = 0  # messages received and forwarded

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def bot_user(self) -> str | None:
        return self._bot_user

    async def _status(self, msg: str) -> None:
        """Emit a status message via callback (if configured)."""
        if self._on_status:
            try:
                await self._on_status(msg)
            except Exception:
                pass

    def _editor(self, channel):
        import discord

        from novacode_cli.remote.discord_format import render
        from novacode_cli.remote.streaming import PagedEditor

        mentions = discord.AllowedMentions.none()

        async def send(body):
            return await channel.send(body, allowed_mentions=mentions)

        async def edit(message, body):
            await message.edit(content=body, allowed_mentions=mentions)

        async def delete(message):
            await message.delete()

        return PagedEditor(render, send, edit, delete)

    async def stream_target(self, *, sid: str = "root") -> RemoteMessage | None:
        if not self.is_connected or self._client is None:
            return None
        channel = self._client.get_channel(int(self._config.chat_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(self._config.chat_id))
        from novacode_cli.remote.discord_format import render

        async def reply(text):
            import discord

            if not self.is_connected:
                return
            for page in render(text):
                await channel.send(page, allowed_mentions=discord.AllowedMentions.none())

        from novacode_cli.remote.streaming import connected_editor

        return RemoteMessage(
            platform=RemotePlatform.DISCORD,
            chat_id=self._config.chat_id,
            user_name="Nova",
            text="",
            reply_fn=reply,
            edit_fn=connected_editor(self, self._editor(channel)),
            answer_edit_fn=connected_editor(self, self._editor(channel)),
            route={"sid": sid},
        )

    async def run(self) -> None:
        """Start the Discord bot and run until cancelled."""
        try:
            import discord
        except ImportError:
            self._last_error = "discord.py is not installed. Install with: pip install discord.py"
            logger.error(self._last_error)
            return

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        intents.dm_messages = True

        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready() -> None:
            self._bot_user = str(client.user)
            self._connected = True
            self._last_error = None
            guilds = len(client.guilds)
            logger.info(f"Discord bridge connected as {client.user} (guilds: {guilds})")
            await self._status(f"Discord bridge connected as {client.user} ({guilds} server(s))")
            self._ready_event.set()

        @client.event
        async def on_message(message: discord.Message) -> None:
            # Log all messages at debug level
            logger.debug(
                f"Discord on_message: author={message.author}, "
                f"channel={message.channel.id}, "
                f"content_len={len(message.content)}, "
                f"is_bot={message.author.bot}"
            )

            # Ignore our own messages
            if message.author == client.user:
                return
            if message.author.bot:
                return
            # Shared guild channels need explicit sender authorization. Private
            # DMs are already scoped to their channel counterpart.
            if getattr(message, "guild", None) is not None and str(message.author.id) not in {
                str(i) for i in self._config.allowed_user_ids
            }:
                return

            # Only process messages in the allowlisted channel
            channel_id_str = str(message.channel.id)
            allowed = {str(self._config.chat_id)} | {str(i) for i in self._config.allowed_ids}

            if channel_id_str not in allowed:
                # Log filtered messages at debug level for troubleshooting
                logger.debug(
                    f"Discord: filtered message from {message.author} "
                    f"in channel {message.channel.id} "
                    f"(allowed: {allowed})"
                )
                return

            # Check message content
            content = message.content.strip()
            if not content:
                # Empty content = embed/attachment only OR missing Message Content Intent
                # Log at warning level since this is a common misconfiguration
                logger.warning(
                    f"Discord: empty content from {message.author} in "
                    f"#{getattr(message.channel, 'name', 'DM')} — "
                    f"if this fires for every message, enable Message Content Intent "
                    f"in https://discord.com/developers/applications > Bot > "
                    f"Privileged Gateway Intents"
                )
                await self._status(
                    f"⚠ Received empty message from {message.author} — "
                    f"if this is a text message, enable Message Content Intent "
                    f"in your bot settings"
                )
                return

            self._msg_count += 1
            logger.info(
                f"Discord message from {message.author} in "
                f"#{getattr(message.channel, 'name', '?')}: "
                f"{content[:80]}{'...' if len(content) > 80 else ''}"
            )
            await self._status(f"📨 Discord message from {message.author}: {content[:100]}")

            async def reply_fn(response_text: str) -> None:
                """Send the agent's response back to the Discord channel."""
                from novacode_cli.remote.discord_format import render

                chunks = render(response_text)
                for chunk in chunks:
                    try:
                        await message.channel.send(
                            chunk, allowed_mentions=discord.AllowedMentions.none()
                        )
                    except discord.HTTPException as e:
                        logger.error(f"Discord send error: {e}")
                    except Exception as e:
                        logger.error(f"Unexpected Discord send error: {e}")

            async def react_fn(emoji: str) -> None:
                """Add a reaction emoji to the user's message (best-effort)."""
                try:
                    await message.add_reaction(emoji)
                except Exception as e:  # noqa: BLE001 — reactions are optional
                    logger.debug(f"Discord reaction failed: {e}")

            edit_fn = self._editor(message.channel)
            answer_edit_fn = self._editor(message.channel)

            ping_enabled = getattr(self._config, "ping", True)
            user_mention = message.author.mention if ping_enabled else None

            try:
                remote_msg = RemoteMessage(
                    platform=RemotePlatform.DISCORD,
                    chat_id=message.channel.id,
                    user_name=str(message.author),
                    text=content,
                    reply_fn=reply_fn,
                    typing_fn=message.channel.typing,
                    react_fn=react_fn,
                    edit_fn=edit_fn,
                    answer_edit_fn=answer_edit_fn,
                    user_mention=user_mention,
                )
            except Exception as _e:
                logger.error(f"Failed to create RemoteMessage: {_e}")
                # Fallback: create without typing_fn
                remote_msg = RemoteMessage(
                    platform=RemotePlatform.DISCORD,
                    chat_id=message.channel.id,
                    user_name=str(message.author),
                    text=content,
                    reply_fn=reply_fn,
                    react_fn=react_fn,
                    edit_fn=edit_fn,
                    answer_edit_fn=answer_edit_fn,
                    user_mention=user_mention,
                )

            await self._queue.put(remote_msg)
            await _debug_log(f"BRIDGE PUT queue={id(self._queue)} text={content[:80]}")
            logger.info(f"Message queued (size: {self._queue.qsize()})")

        @client.event
        async def on_error(event_name: str, *args: Any, **kwargs: Any) -> None:
            """Log gateway errors for diagnostics."""
            logger.error(f"Discord gateway error in event '{event_name}'")

        try:
            async with client:
                await client.start(self._config.token)
        except discord.LoginFailure:
            self._last_error = (
                "Discord login failed — the bot token is invalid. "
                "Check your token at https://discord.com/developers/applications"
            )
            logger.error(self._last_error)
            await self._status(f"❌ {self._last_error}")
        except discord.PrivilegedIntentsRequired:
            self._last_error = (
                "Discord requires Privileged Intents. "
                "Enable 'Message Content Intent' in your bot's settings at "
                "https://discord.com/developers/applications → Bot → "
                "Privileged Gateway Intents"
            )
            logger.error(self._last_error)
            await self._status(f"❌ {self._last_error}")
        except asyncio.CancelledError:
            logger.info("Discord bridge cancelled, shutting down")
        except Exception as e:
            self._last_error = f"Discord bridge error: {e}"
            logger.error(self._last_error)
            await self._status(f"❌ {self._last_error}")
        finally:
            self._connected = False
            if not client.is_closed():
                await client.close()
            logger.info("Discord bridge stopped")

    async def create_channel(
        self, channel_name: str, guild_id: int | None = None
    ) -> tuple[bool, str, str]:
        """Create a text channel in a Discord guild.

        Must be called after the bridge is connected (wait_for_ready has succeeded).

        Args:
            channel_name: Name for the new channel (will be sanitized for Discord).
            guild_id: Specific guild ID. If None, uses the first guild the bot is in.

        Returns:
            (success, channel_id, error_message) tuple. channel_id is str on success.
        """
        try:
            import discord
        except ImportError:
            return False, "", "discord.py is not installed"

        if not self._client or not self._client.is_ready():
            return False, "", "Bridge is not connected yet"

        # Find the target guild
        guild = None
        if guild_id is not None:
            guild = self._client.get_guild(guild_id)
            if guild is None:
                return False, "", f"Guild {guild_id} not found"
        else:
            guilds = self._client.guilds
            if not guilds:
                return False, "", "Bot is not in any server. Invite it first."
            guild = guilds[0]

        # Check for Manage Channels permission
        me = guild.me
        if not me.guild_permissions.manage_channels:
            return False, (
                "",
                "Bot lacks 'Manage Channels' permission. "
                "Invite the bot with: &permissions=274877975552&scope=bot "
                "or enable the permission in server settings.",
            )

        # Sanitize channel name for Discord (lowercase, no spaces, etc.)
        safe_name = channel_name.lower().strip()
        safe_name = re.sub(r"[^a-z0-9-]", "-", safe_name)
        safe_name = re.sub(r"-{2,}", "-", safe_name)
        safe_name = safe_name.strip("-")
        if len(safe_name) > 100:
            safe_name = safe_name[:100]
        if not safe_name:
            safe_name = "nova-code"

        # Check if channel already exists
        existing = discord.utils.get(guild.text_channels, name=safe_name)
        if existing:
            await self._status(f"Channel #{safe_name} already exists — using it")
            return True, str(existing.id), ""

        # Create the channel
        try:
            channel = await guild.create_text_channel(
                safe_name, reason="Auto-created by Nova-Code remote bridge"
            )
            await self._status(f"Created channel #{safe_name} in {guild.name}")
            logger.info(f"Created Discord channel #{safe_name} ({channel.id}) in {guild.name}")
            return True, str(channel.id), ""
        except discord.Forbidden:
            return False, "", "Bot lacks permission to create channels"
        except discord.HTTPException as e:
            return False, "", f"Failed to create channel: {e}"

    async def wait_for_ready(self, timeout: float = 15.0) -> bool:
        """Wait for the Discord gateway connection to be established."""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        """Gracefully stop the Discord client."""
        if self._client and not self._client.is_closed():
            await self._client.close()
