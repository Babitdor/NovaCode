"""Dedicated Telegram intake for an existing Nova Kanban board."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def connect_telegram(session_state):
    """Reuse a bridge or start one from protected saved configuration."""
    from novacode_cli.remote.bridge import RemoteBridgeManager, RemotePlatform
    from novacode_cli.remote.config import async_get_telegram_config

    manager = getattr(session_state, "_remote_bridge_manager", None)
    bridges = manager.bridges(RemotePlatform.TELEGRAM) if manager else []
    if len(bridges) == 1:
        return bridges[0]
    saved = await async_get_telegram_config() or {}
    token, chat = saved.get("token"), saved.get("chat_id")
    if not token or not chat:
        raise ValueError("Configure Telegram with /remote first, then retry this toggle.")
    for bridge in bridges:
        if str(bridge._config.chat_id) == str(chat):
            return bridge
    if manager is None:
        queue = getattr(session_state, "_remote_message_queue", None)
        if queue is None:
            queue = asyncio.Queue()
            session_state._remote_message_queue = queue
        manager = RemoteBridgeManager(queue)
        session_state._remote_bridge_manager = manager
    success, _error = await manager.start_telegram(token=token, chat_id=int(chat))
    if not success:
        # Never expose an API exception containing a bot token to the board.
        raise RuntimeError("Telegram could not connect. Check /remote status and retry.")
    bridges = manager.bridges(RemotePlatform.TELEGRAM)
    return next(bridge for bridge in bridges if str(bridge._config.chat_id) == str(chat))


class TrelloRemote:
    def __init__(self, server, connect, board_id):
        self.server = server
        self.connect = connect
        self.board_id = board_id
        self.loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._tasks = set()
        self.bridge = None
        self.topic_id = None

    def request(self, enabled):
        # HTTP handlers run on a separate thread; all bridge operations belong
        # on the owning asyncio loop. The server records intent immediately.
        self.loop.call_soon_threadsafe(self._schedule)

    def _schedule(self):
        task = self.loop.create_task(self._apply())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _apply(self):
        async with self._lock:
            if not self.server.remote_requested or not self.server.is_running:
                self.server.set_remote_state("off")
                return
            if (
                self.bridge is not None
                and self.bridge.is_connected
                and self.server.get_state()["remote"]["status"] == "connected"
            ):
                return
            self.server.set_remote_state("connecting")
            try:
                bridge = await asyncio.wait_for(self.connect(), 20)
                topic = await asyncio.wait_for(
                    bridge.ensure_session_topic(
                        f"trello:{self.board_id}",
                        f"trello-{self.board_id}",
                        "Trello board",
                        verify=True,
                    ),
                    20,
                )
                if topic is None:
                    raise RuntimeError(
                        "Enable Telegram Topics and grant the bot Manage Topics permission."
                    )
                self.bridge, self.topic_id = bridge, topic
                bridge.set_topic_handler(topic, self.receive)
                if not self.server.remote_requested or not self.server.is_running:
                    self.server.set_remote_state("off")
                    return
                chat = str(bridge._config.chat_id)
                url = f"https://t.me/c/{chat[4:]}/{topic}" if chat.startswith("-100") else None
                self.server.set_remote_state("connected", topic_id=topic, topic_url=url)
                await asyncio.wait_for(
                    bridge.post(
                        "📋 Trello board connected. Send one task per message (or /add <task>). "
                        "Tasks appear in Loaded on the local board. /status shows counts. "
                        "Turning the board's Telegram toggle off pauses task intake.",
                        thread_id=topic,
                        sid=f"trello:{self.board_id}",
                    ),
                    timeout=10,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Trello Telegram connection failed (%s)", type(error).__name__)
                safe_errors = {
                    "Configure Telegram with /remote first, then retry this toggle.",
                    "Enable Telegram Topics and grant the bot Manage Topics permission.",
                }
                message = (
                    str(error)
                    if str(error) in safe_errors
                    else "Telegram could not connect. Check /remote status and retry."
                )
                if self.server.remote_requested and self.server.is_running:
                    self.server.set_remote_state("error", error=message)
                else:
                    self.server.set_remote_state("off")

    async def receive(self, msg):
        # This handler remains registered when intake is paused or the board
        # stops, so messages from this topic never become agent prompts.
        if (
            self.bridge is None
            or str(msg.chat_id) != str(self.bridge._config.chat_id)
            or msg.thread_id != self.topic_id
        ):
            return
        if not self.server.remote_requested or not self.server.is_running:
            await msg.reply_fn(
                "Trello task intake is off. Turn on Telegram in the local board to add tasks."
            )
            return
        text = (msg.text or "").strip()
        parts = text.split(maxsplit=1)
        command = parts[0] if parts else ""
        remainder = parts[1] if len(parts) > 1 else ""
        command = command.lower().split("@", 1)[0]
        if command in ("/help", "/start", "/trello"):
            await msg.reply_fn(
                "Send one task per text message, or /add <task>. /status shows board counts."
            )
            return
        if command == "/status":
            counts = self.server.get_task_counts()
            await msg.reply_fn(
                f"📋 Loaded: {counts['loaded']} · Processing: {counts['processing']} · Done: {counts['done']}"
            )
            return
        if command == "/add":
            text = remainder.strip()
        elif text.startswith("/"):
            await msg.reply_fn(
                "This topic adds Trello cards. Send plain text, /add <task>, or /status."
            )
            return
        if not text or msg.images:
            await msg.reply_fn(
                "Send a text task here. Images are supported in Nova session topics."
            )
            return
        task, created = self.server.add_remote_task(
            text,
            chat_id=msg.chat_id,
            thread_id=msg.thread_id,
            message_id=msg.message_id,
            sender=msg.user_name,
        )
        await msg.reply_fn(
            f"📋 {'Added to Loaded' if created else 'Already added'} · {task['id'][:8]}\n{task['description'][:200]}"
        )
