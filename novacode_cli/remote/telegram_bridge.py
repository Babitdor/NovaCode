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
import hashlib
import logging
import time
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
        self._retry_at = 0.0
        self._last_api_error: str | None = None
        self._session_topics: dict[str, tuple[str, int]] = {}
        self._topic_lock = asyncio.Lock()
        self._keyboard_lock = asyncio.Lock()
        self._shared_polling = None
        self._on_status = None
        self._last_sender_warning = 0.0

    def enable_shared_polling(self) -> None:
        from novacode_cli.remote.telegram_hub import SharedTelegramPolling

        async def fetch(offset):
            self._offset = offset
            return await self._get_updates()

        self._shared_polling = SharedTelegramPolling(self._config.token, fetch)
        self._shared_polling.bindings = [(int(self._config.chat_id), 0)]

    async def ensure_session_topic(self, sid: str, session_id: str, name: str) -> int | None:
        """Bind a process-local route to a durable, bot-scoped session topic."""
        from novacode_cli.memory.store import get_durable_store

        async with self._topic_lock:
            cached = self._session_topics.get(sid)
            if cached and cached[0] == session_id:
                return cached[1]
            if self._shared_polling:
                await self._shared_polling.reserve_session(self._config.chat_id, session_id)
            store = await asyncio.to_thread(get_durable_store)
            bot = hashlib.sha256(self._config.token.encode()).hexdigest()
            namespace = ("nova", "telegram_topics", bot, str(self._config.chat_id))
            saved = await store.aget(namespace, session_id)
            tid = saved.value.get("topic") if saved else None
            if isinstance(tid, int) and not isinstance(tid, bool) and tid > 0:
                # Reopening validates the saved ID and makes a closed session
                # usable again. Already-open topics report TOPIC_NOT_MODIFIED.
                result = await self._api_call(
                    "reopenForumTopic",
                    {"chat_id": self._config.chat_id, "message_thread_id": tid},
                    req_timeout=aiohttp.ClientTimeout(total=10),
                    return_errors=True,
                )
                description = str((result or {}).get("description", "")).upper()
                bad_request = (result or {}).get("error_code") == 400
                missing = bad_request and any(
                    marker in description
                    for marker in (
                        "TOPIC_ID_INVALID",
                        "MESSAGE_THREAD_NOT_FOUND",
                        "MESSAGE THREAD NOT FOUND",
                        "TOPIC_NOT_FOUND",
                        "TOPIC NOT FOUND",
                    )
                )
                already_open = bad_request and any(
                    marker in description
                    for marker in (
                        "TOPIC_NOT_MODIFIED",
                        "TOPIC_NOT_CLOSED",
                    )
                )
                if missing:
                    tid = None
                elif not result or (not result.get("ok") and not already_open):
                    raise RuntimeError(
                        "Could not verify this session's Telegram topic. "
                        + (description or "Try reconnecting.")
                    )
                elif saved.value.get("closed"):
                    await store.aput(namespace, session_id, {"topic": tid})
            if not isinstance(tid, int) or isinstance(tid, bool) or tid <= 0:
                tid = await self.open_topic(f"NOVA · {session_id} · {name}")
                if tid is None:
                    return None
                await store.aput(namespace, session_id, {"topic": tid})
            proposed = {**self._session_topics, sid: (session_id, tid)}
            if self._shared_polling:
                await self._shared_polling.subscribe(
                    [
                        (int(self._config.chat_id), 0),
                        *[(int(self._config.chat_id), value[1]) for value in proposed.values()],
                    ]
                )
            self._session_topics = proposed
            return tid

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
        return_errors: bool = False,
    ) -> dict[str, Any] | None:
        """Make a Telegram Bot API call.

        Args:
            method: API method name (e.g., "getUpdates", "sendMessage").
            payload: Request body as a dict.
            req_timeout: Optional per-request aiohttp timeout. Defaults to the
                session's default (300 s). Long-poll callers should pass
                ``_LONG_POLL_CLIENT_TIMEOUT`` so a network hang is detected
                within ~35 seconds instead of ~5 minutes.
            return_errors: Return Telegram's error response for topic validation,
                so missing topics can be distinguished from other failures.

        Returns:
            Parsed JSON response, or None on error.
        """
        session = self._ensure_session()
        delay = self._retry_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        url = f"{_TELEGRAM_API}/bot{self._config.token}/{method}"
        try:
            async with session.post(url, json=payload, timeout=req_timeout) as resp:
                data = await resp.json()
                self._last_api_error = None
                if not data.get("ok"):
                    description = str(data.get("description", ""))
                    self._last_api_error = description
                    if data.get("error_code") == 429:
                        retry_after = (data.get("parameters") or {}).get("retry_after", 1)
                        self._retry_at = time.monotonic() + max(1, float(retry_after))
                    if (
                        method == "editMessageText"
                        and "message is not modified" in description.lower()
                    ):
                        return {"ok": True, "result": {"message_id": payload.get("message_id")}}
                    if return_errors:
                        return data
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
            if info is None:
                return False  # transient API errors must not disable topics permanently
            chat = info.get("result", {})
            if chat.get("type") == "private":
                me = await self._api_call("getMe", {})
                if me is None:
                    return False
                self.is_forum = bool(me.get("result", {}).get("has_topics_enabled"))
            else:
                self.is_forum = bool(chat.get("is_forum"))
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
        result = await self._api_call(
            "closeForumTopic", {"chat_id": self._config.chat_id, "message_thread_id": thread_id}
        )
        if result:
            from novacode_cli.memory.store import get_durable_store

            store = await asyncio.to_thread(get_durable_store)
            bot = hashlib.sha256(self._config.token.encode()).hexdigest()
            namespace = ("nova", "telegram_topics", bot, str(self._config.chat_id))
            for sid, (session_id, tid) in list(self._session_topics.items()):
                if tid != thread_id:
                    continue
                await store.aput(namespace, session_id, {"topic": tid, "closed": True})
                del self._session_topics[sid]
                if self._shared_polling:
                    await self._shared_polling.release_session(
                        self._config.chat_id,
                        session_id,
                        [
                            (int(self._config.chat_id), 0),
                            *[
                                (int(self._config.chat_id), value[1])
                                for value in self._session_topics.values()
                            ],
                        ],
                    )

    async def post(self, text: str, *, thread_id: int | None = None, sid: str = "root") -> None:
        """Send markdown ``text`` unprompted (e.g. "session started") to a topic."""
        await self._send_message(self._config.chat_id, text, thread_id=thread_id, sid=sid)

    async def post_keyboard(
        self,
        text: str,
        choices: list[list[str]],
        *,
        thread_id: int | None = None,
        sid: str = "root",
        reply_to_message_id: int | None = None,
    ) -> bool:
        """Send a plain-text prompt with a Telegram reply keyboard."""
        keyboard = [[{"text": choice} for choice in row] for row in choices if row]
        payload = self._thread_params(
            {
                "chat_id": self._config.chat_id,
                "text": text,
                "reply_markup": {
                    "keyboard": keyboard,
                    "resize_keyboard": True,
                    "one_time_keyboard": True,
                    "is_persistent": False,
                },
            },
            thread_id,
        )
        self._target_keyboard_reply(payload, reply_to_message_id)
        return await self._send_keyboard_payload(payload, sid)

    async def _send_keyboard_payload(self, payload: dict, sid: str) -> bool:
        # Preserve UI transition order when expiry and incoming commands overlap.
        async with self._keyboard_lock:
            sent = await self._api_call("sendMessage", payload)
            self._remember(sent, sid)
            return sent is not None

    @staticmethod
    def _target_keyboard_reply(payload: dict, message_id: int | None) -> None:
        if message_id is not None:
            payload["reply_parameters"] = {"message_id": message_id}
            payload["reply_markup"]["selective"] = True

    async def remove_keyboard(
        self,
        text: str,
        *,
        thread_id: int | None = None,
        sid: str = "root",
        reply_to_message_id: int | None = None,
    ) -> bool:
        """Retire a picker rather than merely hiding its buttons."""
        payload = self._thread_params(
            {
                "chat_id": self._config.chat_id,
                "text": text,
                "reply_markup": {"remove_keyboard": True},
            },
            thread_id,
        )
        self._target_keyboard_reply(payload, reply_to_message_id)
        return await self._send_keyboard_payload(payload, sid)

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
        if sent is None and not self._last_api_error and self._retry_at <= time.monotonic():
            sent = await self._api_call(method, {**params, "text": to_plain(body)})
        elif sent is None and self._last_api_error and "parse" in self._last_api_error.lower():
            sent = await self._api_call(method, {**params, "text": to_plain(body)})
        return sent

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        sid: str | None = None,
    ) -> list[int]:
        """Send markdown ``text`` as Telegram HTML (chunked, topic-aware).

        Returns the ids of the messages sent, so a caller can delete them.
        """
        ids: list[int] = []
        for chunk in await asyncio.to_thread(render_markdown, text):
            sent = await self._send_html(
                "sendMessage", self._thread_params({"chat_id": chat_id}, thread_id), chunk
            )
            self._remember(sent, sid)
            mid = (sent or {}).get("result", {}).get("message_id")
            if mid is not None:
                ids.append(mid)
        return ids

    async def _delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        """Delete the bot's own messages. Best-effort: Telegram refuses after 48 h."""
        for mid in message_ids:
            try:
                await self._api_call("deleteMessage", {"chat_id": chat_id, "message_id": mid})
            except Exception as e:  # noqa: BLE001 — a leftover message is not worth a crash
                logger.debug(f"Telegram deleteMessage failed for {mid}: {e}")
            self._owner.pop(mid, None)

    def _editor(self, chat_id: int, route: dict):
        from novacode_cli.remote.streaming import PagedEditor

        async def send(body):
            sent = await self._send_html(
                "sendMessage", self._thread_params({"chat_id": chat_id}, route.get("thread")), body
            )
            self._remember(sent, route.get("sid"))
            mid = (sent or {}).get("result", {}).get("message_id")
            if mid is None:
                raise RuntimeError("Telegram could not send the streamed message")
            return mid

        async def edit(mid, body):
            sent = await self._send_html(
                "editMessageText", {"chat_id": chat_id, "message_id": mid}, body
            )
            if sent is None:
                raise RuntimeError("Telegram could not edit the streamed message")

        async def delete(mid):
            await self._delete_messages(chat_id, [mid])

        return PagedEditor(render_markdown, send, edit, delete)

    async def stream_target(self, *, sid: str = "root") -> RemoteMessage | None:
        if not self.is_connected:
            return None
        topic = self._session_topics.get(sid)
        thread = topic[1] if topic else None
        route = {"sid": sid, "thread": thread}
        chat_id = int(self._config.chat_id)

        async def reply(text):
            if self.is_connected:
                await self._send_message(chat_id, text, sid=sid, thread_id=thread)

        from novacode_cli.remote.streaming import connected_editor

        return RemoteMessage(
            platform=RemotePlatform.TELEGRAM,
            chat_id=chat_id,
            user_name="Nova",
            text="",
            reply_fn=reply,
            edit_fn=connected_editor(self, self._editor(chat_id, route)),
            answer_edit_fn=connected_editor(self, self._editor(chat_id, route)),
            route=route,
        )

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
                updates = (
                    await self._shared_polling.next_updates()
                    if self._shared_polling
                    else await self._get_updates()
                )

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
                    text = (message.get("text") or message.get("caption") or "").strip()
                    # A voice note carries no `text`; it is transcribed below,
                    # AFTER the allowlist check, so an unauthorized chat can
                    # never make us fetch and decode a file.
                    voice = message.get("voice") or {}
                    photos = message.get("photo") or []
                    document = message.get("document") or {}
                    image_file = (
                        document
                        if str(document.get("mime_type", "")).startswith("image/")
                        else None
                    )
                    photo = (
                        max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0))
                        if photos
                        else None
                    )
                    attachment = photo or image_file

                    if chat_id is None or (not text and not voice and not attachment):
                        continue

                    # Only process messages from the allowlisted chat.
                    # When in a forum topic, also accept messages from the topic thread.
                    if chat_id != self._config.chat_id and chat_id not in self._config.allowed_ids:
                        continue
                    sender = message.get("from") or {}
                    if sender.get("is_bot"):
                        continue
                    if message.get("chat", {}).get("type") in {
                        "group",
                        "supergroup",
                        "channel",
                    } and str(sender.get("id")) not in {
                        str(i) for i in self._config.allowed_user_ids
                    }:
                        if self._on_status and time.monotonic() - self._last_sender_warning > 10:
                            self._last_sender_warning = time.monotonic()
                            await self._on_status(
                                f"Telegram group message blocked: user {sender.get('id')} is not authorized. "
                                f"In Nova, run /remote allow-user telegram {sender.get('id')} to allow this user."
                            )
                        continue

                    # Registered topics remain identifiable when Telegram omits
                    # is_topic_message; ordinary reply chains aren't topics.
                    thread_id = (
                        message.get("message_thread_id")
                        if message.get("is_topic_message")
                        or any(
                            tid == message.get("message_thread_id")
                            for _, tid in self._session_topics.values()
                        )
                        else None
                    )
                    reply_to = message.get("reply_to_message") or {}
                    reply_owner = self._owner.get(reply_to.get("message_id"))

                    images = []
                    if attachment:
                        image = await self._receive_image(chat_id, attachment, thread_id)
                        if image is None:
                            continue
                        images.append(image)
                        if not text:
                            text = "Please describe this image."
                    elif not text:
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

                    async def ask_fn(
                        question_text: str, _chat_id: int = chat_id, _route: dict = route
                    ):  # noqa: ANN202
                        """Send a question; return a coroutine fn that deletes it."""
                        ids = await self._send_message(
                            _chat_id, question_text, thread_id=_route["thread"], sid=_route["sid"]
                        )

                        async def retract() -> None:
                            await self._delete_messages(_chat_id, ids)

                        return retract

                    async def typing_fn(_chat_id: int = chat_id, _route: dict = route) -> None:
                        await self._trigger_typing(_chat_id, _route["thread"])

                    edit_fn = self._editor(chat_id, route)
                    answer_edit_fn = self._editor(chat_id, route)

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
                        answer_edit_fn=answer_edit_fn,
                        ask_fn=ask_fn,
                        user_mention=user_mention,
                        thread_id=thread_id,
                        reply_to_owner=reply_owner,
                        route=route,
                        sender_id=str(from_user.get("id"))
                        if from_user.get("id") is not None
                        else None,
                        message_id=message.get("message_id"),
                        images=images,
                    )

                    await self._queue.put(remote_msg)

        except asyncio.CancelledError:
            logger.info("Telegram bridge cancelled, shutting down")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Telegram bridge error: {e}")
        finally:
            self._running = False
            self.bot_user = None
            if self._shared_polling:
                await self._shared_polling.close()
            if self._session and not self._session.closed:
                await self._session.close()
            logger.info("Telegram bridge stopped")

    async def _receive_image(self, chat_id: int, attachment: dict, thread_id: int | None):
        """Fetch images only after chat and sender authorization."""
        from novacode_cli.image_utils import MAX_IMAGE_SIZE_BYTES
        from novacode_cli.remote.images import decode_image

        try:
            if attachment.get("file_size", 0) > MAX_IMAGE_SIZE_BYTES:
                raise ValueError("Images must be no larger than 20 MB.")
            file_id = attachment.get("file_id")
            if not file_id:
                raise ValueError("Telegram did not provide an image file ID. Please resend it.")
            data = await self._download_file(file_id, max_bytes=MAX_IMAGE_SIZE_BYTES)
            if data is None:
                raise ValueError("Could not download the image. Please resend it.")
            return await asyncio.to_thread(decode_image, data)
        except ValueError as exc:
            await self._send_message(chat_id, f"Image not sent to Nova: {exc}", thread_id=thread_id)
        except Exception:  # noqa: BLE001 — one bad attachment must not stop polling
            logger.warning("Could not receive Telegram image", exc_info=False)
            await self._send_message(
                chat_id, "Image not sent to Nova. Please resend it.", thread_id=thread_id
            )
        return None

    async def _download_file(self, file_id: str, *, max_bytes: int | None = None) -> bytes | None:
        """Fetch a Telegram file's bytes by ``file_id`` (two-step: getFile, GET)."""
        info = await self._api_call("getFile", {"file_id": file_id})
        file_path = (info or {}).get("result", {}).get("file_path")
        if not file_path:
            return None
        if max_bytes is not None and (info or {}).get("result", {}).get("file_size", 0) > max_bytes:
            raise ValueError("Images must be no larger than 20 MB.")
        url = f"{_TELEGRAM_API}/file/bot{self._config.token}/{file_path}"
        try:
            session = self._ensure_session()
            async with session.get(url) as resp:
                if resp.status != 200:  # noqa: PLR2004
                    logger.error("Telegram file download failed: HTTP %s", resp.status)
                    return None
                if max_bytes is None:
                    return await resp.read()
                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Images must be no larger than 20 MB.")
                return bytes(data)
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001 — a bad download must not kill the poll loop
            logger.error("Telegram file download error: %s", type(e).__name__)
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
