"""Remote image uploads show an attachment indicator in TUI user messages."""

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli.image_utils import ImageData
from novacode_cli.remote.bridge import RemotePlatform
from novacode_cli.tui.widgets import ChatMessage
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


def message(count=1, text="Inspect this screenshot"):
    return SimpleNamespace(
        text=text,
        images=[ImageData("private-image-data", "png", "[image]") for _ in range(count)],
        user_name="alice",
        platform=RemotePlatform.TELEGRAM,
        reply_fn=AsyncMock(),
        typing_fn=None,
    )


@pytest.mark.parametrize(
    "count,text", [(0, "Plain message"), (1, "Caption"), (1, ""), (2, "Caption")]
)
def test_attachment_display_preserves_caption_and_hides_payload(count, text):
    msg = message(count, text)
    body = _app()._remote_user_body(msg).plain
    assert body.startswith(text)
    assert "private-image-data" not in body
    assert msg.text == text and len(msg.images) == count
    if count == 1:
        assert "Image attached" in body
    elif count > 1:
        assert f"{count} images attached" in body
    else:
        assert body == text


async def test_main_remote_image_is_visible_and_reaches_agent_without_display_marker(monkeypatch):
    app = _app()
    msg = message()
    queue = asyncio.Queue()
    stream = AsyncMock()
    monkeypatch.setattr(app, "_remote_route", AsyncMock(return_value=False))
    monkeypatch.setattr(app, "_stream_prompt", stream)
    async with app.run_test():
        app.session_state._remote_message_queue = queue
        worker = app._remote_consumer()
        await queue.put(msg)
        await asyncio.wait_for(queue.join(), 5)
        user = next(entry for entry in app.query(ChatMessage) if entry.has_class("user"))
        assert "Inspect this screenshot" in user.raw_text
        assert "Image attached" in user.raw_text
        assert "private-image-data" not in user.raw_text
        stream.assert_awaited_once_with(msg.text)
        assert app.image_tracker is not None
        worker.cancel()


async def test_child_remote_image_is_visible_and_forwarded_unchanged(monkeypatch):
    app = _app()
    msg = message(2)
    pane = SimpleNamespace(child=object(), status="idle", title="child", remote_turns=deque())
    send = AsyncMock(return_value="prompt-id")
    monkeypatch.setattr(app, "_send_child_prompt", send)
    monkeypatch.setattr(app, "_sync_child_activity", lambda _pane: None)
    monkeypatch.setattr(app, "_refresh_tabs", lambda: None)
    async with app.run_test():
        await app._remote_child_turn(pane, msg)
        user = next(entry for entry in app.query(ChatMessage) if entry.has_class("user"))
        assert "2 images attached" in user.raw_text
        send.assert_awaited_once_with(pane, msg.text, images=msg.images)
