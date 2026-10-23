"""Durable, bot-scoped Discord threads for Nova sessions."""

from __future__ import annotations

import asyncio
import hashlib


class DiscordSessionThreads:
    """Thread lifecycle shared with the TUI's existing topic integration."""

    def _init_session_threads(self) -> None:
        self._session_threads: dict[str, tuple[str, int]] = {}
        self._session_thread_names: dict[str, str] = {}
        self._thread_channels: dict[int, object] = {}
        self._thread_titles: dict[int, str] = {}
        self._joined_threads: set[int] = set()
        self._unsaved_threads: set[int] = set()
        self._thread_lock = asyncio.Lock()

    def _thread_namespace(self) -> tuple[str, ...]:
        bot = hashlib.sha256(self._config.token.encode()).hexdigest()
        return ("nova", "discord_threads", bot, str(self._config.chat_id))

    async def _discord_channel(self, channel_id: int):
        channel = self._client.get_channel(channel_id) or self._thread_channels.get(channel_id)
        return channel if channel is not None else await self._client.fetch_channel(channel_id)

    async def _discord_parent(self):
        """Distinguish an inaccessible configured parent from a deleted session thread."""
        import discord

        try:
            return await self._discord_channel(int(self._config.chat_id))
        except (discord.NotFound, discord.Forbidden) as error:
            raise RuntimeError(
                f"Discord parent channel {self._config.chat_id} is unavailable "
                f"(HTTP {error.status}, Discord code {error.code}). "
                "Check that the channel still exists and the bot is in its server with "
                "View Channel permission. In Discord, enable Developer Mode, right-click "
                "the intended text or forum channel and select Copy Channel ID "
                "(not the server ID). Update the channel in /remote, then restart Discord."
            ) from error

    async def _usable_thread(self, thread, title: str | None = None):
        import discord

        if not isinstance(thread, discord.Thread) or str(thread.parent_id) != str(
            self._config.chat_id
        ):
            raise RuntimeError("Saved Discord thread is outside the configured parent channel.")
        changes = {}
        if thread.archived:
            changes["archived"] = False
        if title is not None and self._thread_titles.get(thread.id) != title:
            if thread.name != title:
                changes["name"] = title
        if changes:
            # Do not unlock moderator-locked threads or replace inaccessible ones.
            thread = await thread.edit(**changes, reason="Resume Nova session")
        if thread.id not in self._joined_threads:
            await thread.join()
            self._joined_threads.add(thread.id)
        if title is not None:
            self._thread_titles[thread.id] = title
        self._thread_channels[thread.id] = thread
        return thread

    async def ensure_session_topic(
        self,
        sid: str,
        session_id: str,
        name: str,
        *,
        verify: bool = False,
    ) -> int | None:
        """Reuse a session thread, creating one only when absent or deleted.

        The method name matches Telegram's adapter so both use the same session
        lifecycle and routing. Discord destinations are threads, not topics.
        """
        import discord

        from novacode_cli.memory.store import get_durable_store

        if not self.is_connected:
            return None
        async with self._thread_lock:
            self._session_thread_names[sid] = name
            parent = await self._discord_parent()
            if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
                return None  # DMs and explicitly configured threads retain legacy behavior.
            label = " ".join(name.split()) or session_id[:8]
            suffix = "" if label == session_id[:8] else f" · {session_id[:8]}"
            title = f"NovaCode · {label[: 100 - len('NovaCode · ') - len(suffix)]}{suffix}"
            cached = self._session_threads.get(sid)
            if cached and cached[0] != session_id:
                # Clearing/resuming the root replaces its durable session ID.
                # Retire its former thread before reusing the local `root` route.
                self._session_threads.pop(sid, None)
                try:
                    previous = await self._discord_channel(cached[1])
                    self._thread_channels[cached[1]] = await previous.edit(
                        archived=True,
                        reason="Nova switched sessions",
                    )
                except discord.NotFound:
                    pass
                store = await asyncio.to_thread(get_durable_store)
                await store.aput(
                    self._thread_namespace(), cached[0], {"thread": cached[1], "closed": True}
                )
            if cached and cached[0] == session_id and not verify:
                thread = self._client.get_channel(cached[1]) or self._thread_channels.get(cached[1])
                if thread is not None:
                    try:
                        if cached[1] in self._unsaved_threads:
                            store = await asyncio.to_thread(get_durable_store)
                            await store.aput(
                                self._thread_namespace(), session_id, {"thread": cached[1]}
                            )
                            self._unsaved_threads.discard(cached[1])
                        await self._usable_thread(thread, title)
                        return cached[1]
                    except discord.NotFound:
                        pass
            store = await asyncio.to_thread(get_durable_store)
            namespace = self._thread_namespace()
            saved = await store.aget(namespace, session_id)
            tid = saved.value.get("thread") if saved else None
            thread = None
            if isinstance(tid, int) and not isinstance(tid, bool) and tid > 0:
                try:
                    thread = await self._client.fetch_channel(tid)
                    thread = await self._usable_thread(thread, title)
                except discord.NotFound:
                    thread = None
            if thread is None:
                if isinstance(parent, discord.ForumChannel):
                    created = await parent.create_thread(
                        name=title,
                        content=f"Nova session {session_id}. Messages here go to this session.",
                        allowed_mentions=discord.AllowedMentions.none(),
                        auto_archive_duration=1440,
                        reason="Nova session remote thread",
                    )
                    thread = created.thread
                else:
                    thread = await parent.create_thread(
                        name=title,
                        type=discord.ChannelType.public_thread,
                        auto_archive_duration=1440,
                        reason="Nova session remote thread",
                    )
                # Retain successful creation even if saving or joining needs a
                # retry. A transient API failure must not create duplicate threads.
                self._session_threads[sid] = (session_id, thread.id)
                self._thread_channels[thread.id] = thread
                self._thread_titles[thread.id] = title
                self._unsaved_threads.add(thread.id)
                await store.aput(namespace, session_id, {"thread": thread.id})
                self._unsaved_threads.discard(thread.id)
                await thread.join()
                self._joined_threads.add(thread.id)
                if isinstance(parent, discord.TextChannel):
                    try:
                        announcement = await parent.send(
                            f"Nova session **{name}** → <#{thread.id}>",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        self._remember_owner(announcement, {"sid": sid})
                    except discord.HTTPException:
                        await self._status(
                            "Discord session thread created, but its parent-channel link could not be posted."
                        )
            else:
                await store.aput(namespace, session_id, {"thread": thread.id})
            self._session_threads[sid] = (session_id, thread.id)
            self._thread_channels[thread.id] = thread
            return thread.id

    async def close_topic(self, thread_id: int) -> None:
        """Archive a closed session's thread, preserving its history for resume."""
        from novacode_cli.memory.store import get_durable_store

        async with self._thread_lock:
            thread = await self._discord_channel(thread_id)
            if str(getattr(thread, "parent_id", "")) != str(self._config.chat_id):
                raise RuntimeError("Cannot archive a thread outside the configured channel.")
            thread = await thread.edit(archived=True, reason="Nova session closed")
            self._thread_channels[thread_id] = thread
            store = await asyncio.to_thread(get_durable_store)
            for sid, (session_id, tid) in list(self._session_threads.items()):
                if tid == thread_id:
                    await store.aput(
                        self._thread_namespace(), session_id, {"thread": tid, "closed": True}
                    )
                    del self._session_threads[sid]
                    self._session_thread_names.pop(sid, None)

    async def _session_destination(self, sid: str):
        """Resolve the live session thread; only the root may fall back to its parent."""
        import discord

        binding = self._session_threads.get(sid)
        if binding:
            thread = await self._discord_channel(binding[1])
            # Sending can automatically unarchive, but edits cannot. Restore first.
            return await self._usable_thread(thread)
        if sid != "root":
            return None
        parent = await self._discord_parent()
        return None if isinstance(parent, discord.ForumChannel) else parent

    def _incoming_destination(self, channel) -> tuple[str | int, int | None] | None:
        """Accept exact allowed channels and our registered threads, never siblings."""
        allowed = {str(self._config.chat_id), *map(str, self._config.allowed_ids)}
        if str(channel.id) in allowed:
            return channel.id, None
        tid = channel.id
        if tid not in self._thread_channels:
            return None
        if str(getattr(channel, "parent_id", "")) != str(self._config.chat_id):
            return None
        return self._config.chat_id, tid
