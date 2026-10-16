"""/stop: stop the running task from Telegram/Discord (or the chat), like Esc."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli.remote.bridge import RemotePlatform
from novacode_cli.remote.processor import is_stop_command, remote_message_processor


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        platform=RemotePlatform.TELEGRAM, chat_id=7, thread_id=None, sender_id="alice",
        user_name="alice", text=text, images=[], reply_fn=AsyncMock(), react_fn=None,
        typing_fn=None, edit_fn=None, answer_edit_fn=None,
    )


def test_stop_and_its_forms():
    assert is_stop_command("/stop") and is_stop_command(" /STOP ") and is_stop_command("/cancel")
    assert is_stop_command("/stop@NovaBot")
    assert not is_stop_command("/stopwatch") and not is_stop_command("stop") and not is_stop_command("/steer stop")


@pytest.mark.asyncio
async def test_headless_stop_cancels_the_running_turn():
    started = asyncio.Event()

    async def endless_turn(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    class Agent:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": []})

    session = SimpleNamespace(thread_id="t", auto_approve=False, session_id="s")
    queue: asyncio.Queue = asyncio.Queue()
    console = SimpleNamespace(print=lambda *a, **k: None)
    processor = asyncio.create_task(remote_message_processor(
        queue, Agent(), "nova", session, console, None, None, None, set(), execute_fn=endless_turn,
    ))
    try:
        await queue.put(_message("build it"))
        await asyncio.wait_for(started.wait(), 5)
        stop = _message("/stop")
        await queue.put(stop)
        await asyncio.wait_for(queue.join(), 5)
        stop.reply_fn.assert_awaited_with("⏹ Stopped.")
        assert not session._remote_message_lock.locked(), "the turn let go of the session"

        again = _message("/stop")
        await queue.put(again)
        await asyncio.wait_for(queue.join(), 5)
        again.reply_fn.assert_awaited_with("Nothing is running.")
    finally:
        processor.cancel()
        await asyncio.gather(processor, return_exceptions=True)


@pytest.mark.asyncio
async def test_chat_app_stops_a_telegram_turn_mid_run_instead_of_queueing_it():
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(110, 40)) as pilot:
        queue: asyncio.Queue = asyncio.Queue()
        app.session_state._remote_message_queue = queue
        turn = asyncio.create_task(asyncio.sleep(3600))  # the Telegram turn in progress
        app._remote_turn_task = turn
        app._turn_active = True
        app._remote_consumer()

        stop = _message("/stop")
        await queue.put(stop)
        await asyncio.wait_for(queue.join(), 5)
        await pilot.pause(0.1)
        assert turn.cancelled()
        stop.reply_fn.assert_awaited_with("⏹ Stopped.")

        app._turn_active = False
        reply, _ = await app._remote_slash("/stop")
        assert reply == "Nothing is running."
