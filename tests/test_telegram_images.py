"""Image authorization, validation, download bounds, and topic preservation."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from novacode_cli.remote.bridge import BridgeConfig, RemotePlatform
from novacode_cli.remote.images import decode_image
from novacode_cli.remote.telegram_bridge import TelegramBridge


def png():
    stream = io.BytesIO()
    Image.new("RGBA", (8, 6), (255, 0, 0, 128)).save(stream, format="PNG")
    return stream.getvalue()


def update(*, caption=None, document=False, chat=99, sender=42):
    message = {
        "chat": {"id": chat, "type": "supergroup"},
        "from": {"id": sender, "username": "user"},
        "message_thread_id": 88,
        "is_topic_message": True,
        "reply_to_message": {"message_id": 5},
    }
    if document:
        message["document"] = {"file_id": "original", "mime_type": "image/png"}
    else:
        message["photo"] = [
            {"file_id": "small", "width": 2, "height": 2},
            {"file_id": "large", "width": 8, "height": 6},
        ]
    if caption is not None:
        message["caption"] = caption
    return {"update_id": 1, "message": message}


def bridge():
    return TelegramBridge(
        BridgeConfig(RemotePlatform.TELEGRAM, "secret", 99, allowed_user_ids={42}),
        asyncio.Queue(),
    )


async def poll_once(bot, updates):
    async def api(method, payload, **kwargs):
        if method == "getMe":
            return {"result": {"username": "nova"}}
        return {"result": {}}

    bot._api_call = AsyncMock(side_effect=api)
    bot._get_updates = AsyncMock(side_effect=[updates, asyncio.CancelledError()])
    await bot.run()


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [False, True])
@pytest.mark.parametrize("caption", [None, "Find the bug in this screenshot"])
async def test_image_and_caption_reach_topic(document, caption):
    bot = bridge()
    bot._owner[5] = "child"
    bot._download_file = AsyncMock(return_value=png())
    await poll_once(bot, [update(document=document, caption=caption)])
    msg = bot._queue.get_nowait()
    assert msg.text == (caption or "Please describe this image.")
    assert msg.thread_id == 88 and msg.reply_to_owner == "child"
    assert msg.sender_id == "42"
    assert len(msg.images) == 1
    assert msg.images[0].to_data_url().startswith("data:image/png;base64,")
    assert "secret" not in repr(msg)
    bot._download_file.assert_awaited_once_with(
        "original" if document else "large", max_bytes=20 * 1024 * 1024
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("chat,sender,is_bot", [(123, 42, False), (99, 7, False), (99, 42, True)])
async def test_unauthorized_images_never_download(chat, sender, is_bot):
    bot = bridge()
    bot._download_file = AsyncMock()
    incoming = update(chat=chat, sender=sender)
    incoming["message"]["from"]["is_bot"] = is_bot
    await poll_once(bot, [incoming])
    bot._download_file.assert_not_called()
    assert bot._queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [None, b"not an image", b""])
async def test_bad_image_reports_failure_in_origin_topic(data):
    bot = bridge()
    bot._download_file = AsyncMock(return_value=data)
    bot._send_message = AsyncMock()
    incoming = update(caption="Do not run this without the picture")
    text_update = {"message": {"chat": {"id": 99}, "from": {"id": 42}, "text": "still here"}}
    await poll_once(bot, [incoming, text_update])
    assert bot._queue.qsize() == 1
    assert bot._queue.get_nowait().text == "still here"
    args = bot._send_message.await_args
    assert "Image not sent" in args.args[1]
    assert args.kwargs["thread_id"] == 88


@pytest.mark.asyncio
async def test_oversized_metadata_rejected_before_download():
    bot = bridge()
    bot._download_file = AsyncMock()
    bot._send_message = AsyncMock()
    incoming = update(document=True)
    incoming["message"]["document"]["file_size"] = 21 * 1024 * 1024
    await poll_once(bot, [incoming])
    bot._download_file.assert_not_called()
    assert bot._queue.empty()
    assert "20 MB" in bot._send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_download_stream_enforces_limit_without_trusting_metadata():
    bot = bridge()
    bot._api_call = AsyncMock(return_value={"result": {"file_path": "photos/image.png"}})

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            yield b"1234"
            yield b"5678"
            raise AssertionError("must stop reading as soon as limit is exceeded")

        @property
        def content(self):
            return self

    bot._ensure_session = lambda: SimpleNamespace(get=lambda url: Response())
    with pytest.raises(ValueError, match="20 MB"):
        await bot._download_file("file", max_bytes=5)


def test_image_normalization_and_size_limits(monkeypatch):
    import novacode_cli.remote.images as module

    image = decode_image(png())
    assert image.format == "png"
    monkeypatch.setattr(module, "MAX_IMAGE_DIMENSION", 5)
    with pytest.raises(ValueError, match="dimensions"):
        decode_image(png())
    monkeypatch.setattr(module, "MAX_IMAGE_SIZE_BYTES", 1)
    with pytest.raises(ValueError, match="20 MB"):
        decode_image(png())


@pytest.mark.asyncio
async def test_headless_remote_images_wait_for_lock_and_reach_model(monkeypatch):
    from novacode_cli.core.input_preparation import prepare_input_content
    from novacode_cli.remote.bridge import RemoteMessage
    from novacode_cli.remote.processor import remote_message_processor

    monkeypatch.setattr("novacode_cli.remote.processor._debug_log", AsyncMock())
    monkeypatch.setattr("novacode_cli.hooks.dispatch_hook_fire_and_forget", lambda *args: None)
    monkeypatch.setattr(
        "novacode_cli.core.input_preparation._main_model_can_see_images", lambda: True
    )
    lock = asyncio.Lock()
    await lock.acquire()
    state = SimpleNamespace(
        thread_id="test",
        session_id="test",
        auto_approve=False,
        _remote_message_lock=lock,
        steering_instructions=[],
    )
    agent = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values={"messages": []}))
    )
    captured = []
    accepted = asyncio.Event()

    async def execute(text, *args, image_tracker=None, **kwargs):
        captured.append(await prepare_input_content(text, image_tracker, skip_file_mentions=True))
        accepted.set()

    queue = asyncio.Queue()
    msg = RemoteMessage(
        RemotePlatform.TELEGRAM,
        99,
        "user",
        "/btw inspect",
        AsyncMock(),
        images=[decode_image(png())],
    )
    running = asyncio.create_task(
        remote_message_processor(
            queue,
            agent,
            "nova",
            state,
            SimpleNamespace(print=lambda *args: None),
            None,
            None,
            None,
            set(),
            execute_fn=execute,
        )
    )
    try:
        await queue.put(msg)
        await asyncio.sleep(0.1)
        assert not captured and not state.steering_instructions
        lock.release()
        await asyncio.wait_for(accepted.wait(), 5)
        await asyncio.wait_for(queue.join(), 5)
        assert captured[0][0]["text"] == "/btw inspect"
        assert captured[0][1] == msg.images[0].to_message_content()
        assert state.auto_approve is False
    finally:
        running.cancel()
        await running
