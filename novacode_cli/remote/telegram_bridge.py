"""Telegram bridge — forwards messages between Telegram and the Nova-Code agent.

Uses the Telegram Bot HTTP API directly via ``aiohttp`` (already a
dependency), so no additional packages are needed.  Long-polling via
``getUpdates`` means we don't need webhooks or incoming ports.

Message Flow
------------
1. Background task calls ``getUpdates`` in a loop (long-polling with
   30-second timeout).
2. Incoming messages from the allowlisted chat are wrapped as
   ``RemoteMessage`` and put on the shared queue.
3. The CLI's main loop runs ``execute_task()`` with the message text.
4. The ``reply_fn`` uses ``sendMessage`` to return the response.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from novacode_cli.remote.bridge import (
    BridgeConfig,
    RemoteMessage,
    RemotePlatform,
)
from novacode_cli.remote.telegram_format import render as render_markdown
from novacode_cli.remote.telegram_format import to_plain

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


_LONG_POLL_SERVER_TIMEOUT = 30  # seconds Telegram holds the connection open
_LONG_POLL_CLIENT_TIMEOUT = aiohttp.ClientTimeout(
    total=_LONG_POLL_SERVER_TIMEOUT + 5
)  # client-side deadline: give server 5 s extra, then treat as a network hang


class TelegramBridge:
    """Telegram bot bridge for Nova-Code.

    Connects to the Telegram Bot API via long-polling.  No additional
    dependencies beyond ``aiohttp`` (already installed).
    """

    def __init__(
        self,
        config: BridgeConfig,
        message_queue: asyncio.Queue[RemoteMessage],
    ) -> None:
        self._config = config
        self._queue = message_queue
        self._session: aiohttp.ClientSession | None = None
        self._offset: int = 0  # last update_id + 1 for long-polling
        self._running = False
        self.is_forum: bool | None = None  # topics enabled? (detected on first use)
        self._owner: dict[int, str] = {}  # sent message_id -> session id that sent it
        self.bot_user: str | None = None  # set after successful getMe

    @property
    def is_connected(self) -> bool:
        """True once the bot is verified and the poll loop is running."""
        return self._running and self.bot_user is not None

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the current session, creating a fresh one if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _api_call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        req_timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any] | None:
        """Make a Telegram Bot API call.

        Args:
            method: API method name (e.g., "getUpdates", "sendMessage").
            payload: Request body as a dict.
            req_timeout: Optional per-request aiohttp timeout. Defaults to the
                session's default (300 s). Long-poll callers should pass
                ``_LONG_POLL_CLIENT_TIMEOUT`` so a network hang is detected
                within ~35 seconds instead of ~5 minutes.

        Returns:
            Parsed JSON response, or None on error.
        """
        session = self._ensure_session()
        url = f"{_TELEGRAM_API}/bot{self._config.token}/{method}"
        try:
            async with session.post(url, json=payload, timeout=req_timeout) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.error(f"Telegram API error: {data.get('description')}")
                    return None
                return data
        except aiohttp.ClientError as e:
            logger.error(f"Telegram HTTP error ({method}): {e}")
            # Close the stale session so the next call opens a fresh connection.
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
            return None
        except TimeoutError:
            logger.debug(f"Telegram long-poll timeout on {method} — normal, retrying")
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
            return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"Telegram API call error ({method}): {e}")
            return None

    async def _get_updates(self) -> list[dict[str, Any]]:
        """Long-poll for updates from Telegram.

        The server holds the connection open for up to ``_LONG_POLL_SERVER_TIMEOUT``
        seconds while waiting for new messages, then responds with an empty list.
        We set a client-side deadline of server_timeout + 5 s so any network hang
        is detected quickly rather than waiting for aiohttp's 5-minute default.
        """
        result = await self._api_call(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": _LONG_POLL_SERVER_TIMEOUT,
                "allowed_updates": ["message"],
            },
            req_timeout=_LONG_POLL_CLIENT_TIMEOUT,
        )
        if result is None:
            return []

        updates = result.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # ── topics: one per Nova session (forum supergroups) ─────────────────

    async def detect_forum(self) -> bool:
        """Whether the chat is a forum supergroup (topics enabled). Cached."""
        if self.is_forum is None:
            info = await self._api_call("getChat", {"chat_id": self._config.chat_id})
            self.is_forum = bool((info or {}).get("result", {}).get("is_forum"))
        return self.is_forum

    async def open_topic(self, name: str) -> int | None:
        """Create a topic for a session; its ``message_thread_id``, or None
        when the chat has no topics or the bot may not manage them."""
        if not await self.detect_forum():
            return None
        result = await self._api_call(
            "createForumTopic", {"chat_id": self._config.chat_id, "name": name[:128]}
        )
        return (result or {}).get("result", {}).get("message_thread_id")

    async def close_topic(self, thread_id: int) -> None:
        """Close (not delete) a session's topic: its history stays readable."""
        await self._api_call(
            "closeForumTopic", {"chat_id": self._config.chat_id, "message_thread_id": thread_id}
        )

    async def post(self, text: str, *, thread_id: int | None = None, sid: str = "root") -> None:
        """Send markdown ``text`` unprompted (e.g. "session started") to a topic."""
        await self._send_message(self._config.chat_id, text, thread_id=thread_id, sid=sid)

    def _remember(self, sent: dict | None, sid: str | None) -> None:
        """Note which session sent a message, so replying to it reaches that session."""
        mid = (sent or {}).get("result", {}).get("message_id")
        if mid is None or not sid:
            return
        self._owner[mid] = sid
        if len(self._owner) > 5000:  # bounded: forget the oldest half
            for key in list(self._owner)[:2500]:
                del self._owner[key]

    def _thread_params(self, base: dict[str, Any], thread_id: int | None = None) -> dict[str, Any]:
        """Inject ``message_thread_id`` into an API payload for a topic."""
        if thread_id is not None:
            return {**base, "message_thread_id": thread_id}
        return base

    async def _send_html(self, method: str, params: dict[str, Any], body: str) -> dict | None:
        """Send/edit ``body`` (Telegram HTML); on a parse refusal, resend it plain.

        Without the fallback a message Telegram rejects is simply lost.
        """
        sent = await self._api_call(method, {**params, "text": body, "parse_mode": "HTML"})
        if sent is None:
            sent = await self._api_call(method, {**params, "text": to_plain(body)})
        return sent

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        sid: str | None = None,
    ) -> None:
        """Send markdown ``text`` as Telegram HTML (chunked, topic-aware)."""
        for chunk in render_markdown(text):
            sent = await self._send_html(
                "sendMessage", self._thread_params({"chat_id": chat_id}, thread_id), chunk
            )
            self._remember(sent, sid)

    async def run(self) -> None:
        """Start the Telegram long-polling loop.

        This is intended to be run as an ``asyncio.Task``.
        """
        self._running = True

        # Get bot info to verify token
        me = await self._api_call("getMe", {})
        if me is None:
            logger.error("Telegram bot token is invalid or API is unreachable")
            self._running = False
            if self._session and not self._session.closed:
                await self._session.close()
            return

        bot_name = me.get("result", {}).get("username", "unknown")
        self.bot_user = bot_name
        logger.info(f"Telegram bridge connected as @{bot_name}")

        _consecutive_errors = 0

        try:
            while self._running:
                updates = await self._get_updates()

                if not updates:
                    # No messages (normal) or a network error (also returns []).
                    # Back off briefly after repeated failures to avoid spinning.
                    if _consecutive_errors > 0:
                        await asyncio.sleep(min(2**_consecutive_errors, 30))
                    _consecutive_errors += 1
                else:
                    _consecutive_errors = 0

                for update in updates:
                    message = update.get("message")
                    if not message:
                        continue

                    chat = message.get("chat", {})
                    chat_id = chat.get("id")
                    from_user = message.get("from", {})
                    user_name = from_user.get("username") or from_user.get("first_name", "unknown")
                    text = (message.get("text") or "").strip()
                    # A voice note carries no `text`; it is transcribed below,
                    # AFTER the allowlist check, so an unauthorized chat can
                    # never make us fetch and decode a file.
                    voice = message.get("voice") or {}

                    if chat_id is None or (not text and not voice):
                        continue

                    # Only process messages from the allowlisted chat.
                    # When in a forum topic, also accept messages from the topic thread.
                    if chat_id != self._config.chat_id and chat_id not in self._config.allowed_ids:
                        continue

                    # Which topic it came from (forum groups only: in plain
                    # groups message_thread_id also marks reply chains).
                    thread_id = (
                        message.get("message_thread_id")
                        if message.get("is_topic_message")
                        else None
                    )
                    reply_to = message.get("reply_to_message") or {}
                    reply_owner = self._owner.get(reply_to.get("message_id"))

                    if not text:
                        text = await self._transcribe_voice(chat_id, voice, thread_id)
                        if not text:
                            continue  # nothing usable; the sender was told why

                    logger.info(
                        f"Telegram message from {user_name}: "
                        f"{text[:80]}{'...' if len(text) > 80 else ''}"
                    )

                    # The router fills ``route`` before anything is sent back:
                    # the session that answers ("sid") and where ("thread").
                    route: dict = {"sid": None, "thread": thread_id}

                    async def reply_fn(
                        response_text: str, _chat_id: int = chat_id, _route: dict = route
                    ) -> None:
                        await self._send_message(
                            _chat_id, response_text, thread_id=_route["thread"], sid=_route["sid"]
                        )

                    async def typing_fn(_chat_id: int = chat_id, _route: dict = route) -> None:
                        await self._trigger_typing(_chat_id, _route["thread"])

                    # Edit-in-place live status. ``body`` is markdown (the status
                    # line's activity log); it is rendered to HTML on every edit,
                    # which is safe mid-stream: markdown-it closes what the text
                    # leaves open. Past one message it rolls over to a new one.
                    _live: dict = {"id": None, "last": None}

                    async def edit_fn(
                        body: str,
                        final: bool = False,  # noqa: ARG001
                        _chat_id: int = chat_id,
                        _route: dict = route,
                        _live: dict = _live,
                    ) -> None:
                        try:
                            pages = render_markdown(body)
                            if len(pages) > 1 and _live["id"] is not None:
                                # Overflowed: freeze this message, continue in a new one.
                                await self._send_html(
                                    "editMessageText",
                                    {"chat_id": _chat_id, "message_id": _live["id"]},
                                    pages[0],
                                )
                                _live["id"] = None
                            page = pages[-1]
                            if page == _live["last"]:
                                return
                            if _live["id"] is None:
                                sent = await self._send_html(
                                    "sendMessage",
                                    self._thread_params({"chat_id": _chat_id}, _route["thread"]),
                                    page,
                                )
                                self._remember(sent, _route["sid"])
                                _live["id"] = (sent or {}).get("result", {}).get("message_id")
                            else:
                                await self._send_html(
                                    "editMessageText",
                                    {"chat_id": _chat_id, "message_id": _live["id"]},
                                    page,
                                )
                            _live["last"] = page
                        except Exception as e:  # noqa: BLE001
                            logger.error(f"Telegram stream edit error: {e}")

                    tg_username = from_user.get("username")
                    user_mention = f"@{tg_username}" if tg_username else None

                    remote_msg = RemoteMessage(
                        platform=RemotePlatform.TELEGRAM,
                        chat_id=chat_id,
                        user_name=user_name,
                        text=text,
                        reply_fn=reply_fn,
                        typing_fn=typing_fn,
                        edit_fn=edit_fn,
                        user_mention=user_mention,
                        thread_id=thread_id,
                        reply_to_owner=reply_owner,
                        route=route,
                    )

                    await self._queue.put(remote_msg)

        except asyncio.CancelledError:
            logger.info("Telegram bridge cancelled, shutting down")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Telegram bridge error: {e}")
        finally:
            self._running = False
            self.bot_user = None
            if self._session and not self._session.closed:
                await self._session.close()
            logger.info("Telegram bridge stopped")

    async def _download_file(self, file_id: str) -> bytes | None:
        """Fetch a Telegram file's bytes by ``file_id`` (two-step: getFile, GET)."""
        info = await self._api_call("getFile", {"file_id": file_id})
        file_path = (info or {}).get("result", {}).get("file_path")
        if not file_path:
            return None
        url = f"{_TELEGRAM_API}/file/bot{self._config.token}/{file_path}"
        try:
            session = self._ensure_session()
            async with session.get(url) as resp:
                if resp.status != 200:  # noqa: PLR2004
                    logger.error("Telegram file download failed: HTTP %s", resp.status)
                    return None
                return await resp.read()
        except Exception as e:  # noqa: BLE001 — a bad download must not kill the poll loop
            logger.error(f"Telegram file download error: {e}")
            return None

    async def _transcribe_voice(
        self, chat_id: int, voice: dict[str, Any], thread_id: int | None = None
    ) -> str:
        """Transcribe a voice note, or explain to the sender why it could not be.

        Every failure path replies. The sender is on a phone, not at the
        terminal: a voice note that vanishes silently is indistinguishable from
        a dead bot, so "I could not hear that" is itself the feature.
        """
        from novacode_cli.remote.voice_notes import (
            VoiceNotesUnavailable,
            VoiceNoteTooLong,
            transcribe_voice_note,
        )

        file_id = voice.get("file_id")
        if not file_id:
            return ""

        # Transcribing is slow enough to look like a hang; show activity first.
        await self._trigger_typing(chat_id)

        data = await self._download_file(file_id)
        if not data:
            await self._send_message(
                chat_id, thread_id=thread_id, text="🎙 Could not download that voice note."
            )
            return ""

        try:
            text = await transcribe_voice_note(data, duration=voice.get("duration"))
        except (VoiceNotesUnavailable, VoiceNoteTooLong) as e:
            await self._send_message(chat_id, thread_id=thread_id, text=f"🎙 {e}")
            return ""
        except Exception:
            # Never kill the poll loop: one bad clip must not stop every later
            # message from being answered.
            logger.exception("Voice note transcription failed")
            await self._send_message(
                chat_id, thread_id=thread_id, text="🎙 Could not transcribe that voice note."
            )
            return ""

        if not text:
            await self._send_message(
                chat_id, "🎙 I could not make out any speech in that note.", thread_id=thread_id
            )
            return ""

        # Echo what was heard before acting on it. Transcription is fallible,
        # and a wrong transcript that silently becomes a prompt is worse than a
        # visible one the sender can correct.
        await self._send_message(chat_id, thread_id=thread_id, text=f"🎙 “{text}”")
        return text

    async def _trigger_typing(self, chat_id: int, thread_id: int | None = None) -> None:
        """Send a 'typing' chat action to Telegram."""
        await self._api_call(
            "sendChatAction",
            self._thread_params({"chat_id": chat_id, "action": "typing"}, thread_id),
        )

    async def stop(self) -> None:
        """Gracefully stop the Telegram bridge."""
        self._running = False
