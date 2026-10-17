"""Up/Down input history, taking back a queued message, and /redirect from a chat."""

from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli.remote.processor import redirect_text


def test_redirect_text():
    assert redirect_text("/redirect use Postgres") == "use Postgres"
    assert redirect_text("/redirect@NovaBot  do X ") == "do X"
    assert redirect_text("/redirect") == ""
    assert redirect_text("/redirection x") is None and redirect_text("hello") is None


async def _box(app, pilot):
    from novacode_cli.tui.widgets import PromptInput

    box = app.query_one("#prompt", PromptInput)
    box.focus()
    await pilot.pause()
    return box


@pytest.mark.asyncio
async def test_up_and_down_walk_earlier_inputs_and_return_to_the_draft():
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        box = await _box(app, pilot)
        for text in ("first", "second", "third"):
            box.remember(text)
        box.text = "half-typed"
        await pilot.press("up")
        assert box.text == "third"
        await pilot.press("up", "up", "up")
        assert box.text == "first", "stops at the oldest"
        await pilot.press("down")
        assert box.text == "second"
        await pilot.press("down", "down")
        assert box.text == "half-typed", "back to what was being typed"


@pytest.mark.asyncio
async def test_inside_multiline_text_the_arrows_still_move_the_cursor():
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        box = await _box(app, pilot)
        box.remember("old")
        box.text = "line one\nline two"
        box.move_cursor((1, 3))
        await pilot.press("up")
        assert box.text == "line one\nline two" and box.cursor_location[0] == 0


@pytest.mark.asyncio
async def test_up_in_an_empty_box_takes_back_a_queued_message():
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._turn_active = True
        app._add_live_steer("use SQLite instead")
        box = await _box(app, pilot)
        box.remember("an older input")
        await pilot.press("up")
        assert box.text == "use SQLite instead"
        assert app._live_steers == [] and not app.session_state.steering_instructions, "out of the queue"
        box.text = ""
        await pilot.press("up")
        assert box.text == "an older input", "with nothing queued, Up is history"


@pytest.mark.asyncio
async def test_a_picked_up_message_cannot_be_taken_back():
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(120, 40)):
        app._turn_active = True
        app._add_live_steer("too late")
        app._live_steers[0].consumed = True  # the agent already read it
        assert app._pull_queued_message() is None


@pytest.mark.asyncio
async def test_sent_inputs_are_kept_for_the_next_session(monkeypatch):
    from novacode_cli.tui.widgets import PromptInput
    from tests.test_tui_compaction_row import _app

    path = pathlib.Path(tempfile.mkdtemp()) / "input_history.jsonl"
    monkeypatch.setenv("NOVA_INPUT_HISTORY", str(path))
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._remember_input(app.query_one("#prompt", PromptInput), "fix the parser")
        for _ in range(20):
            if path.exists():
                break
            await pilot.pause(0.05)
    assert json.loads(path.read_text(encoding="utf-8"))["text"] == "fix the parser"

    later = _app()
    async with later.run_test(size=(120, 40)) as pilot:
        await later._load_input_history()
        box = later.query_one("#prompt", PromptInput)
        box.focus()
        await pilot.press("up")
        assert box.text == "fix the parser"


@pytest.mark.asyncio
async def test_redirect_from_telegram_stops_the_task_and_queues_the_message():
    from novacode_cli.remote.bridge import RemotePlatform
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(110, 40)) as pilot:
        queue: asyncio.Queue = asyncio.Queue()
        app.session_state._remote_message_queue = queue
        turn = asyncio.create_task(asyncio.sleep(3600))
        app._remote_turn_task = turn
        app._turn_active = True
        app._remote_consumer()
        msg = SimpleNamespace(
            platform=RemotePlatform.TELEGRAM, chat_id=7, thread_id=None, sender_id="a", user_name="a",
            text="/redirect use Postgres instead", images=[], reply_fn=AsyncMock(), react_fn=None,
            typing_fn=None,
        )
        await queue.put(msg)
        await asyncio.wait_for(queue.join(), 5)
        await pilot.pause(0.1)
        assert turn.cancelled()
        assert app._redirect_prompt == "use Postgres instead"
        assert "use Postgres instead" in msg.reply_fn.call_args.args[0]
