"""Live responses preserve platform formatting, routing, and UI responsiveness."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.remote.bridge import BridgeConfig, RemoteBridgeManager, RemotePlatform
from novacode_cli.remote.discord_format import render as discord_render
from novacode_cli.remote.streaming import PagedEditor, RemoteAnswerStream
from novacode_cli.remote.telegram_bridge import TelegramBridge
from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401


async def test_pages_grow_shrink_and_retry_without_losing_middle_pages():
    sent, edited, deleted = [], [], []

    async def send(body):
        sent.append(body)
        return len(sent)

    async def edit(mid, body):
        edited.append((mid, body))

    async def delete(mid):
        deleted.append(mid)

    editor = PagedEditor(
        lambda text: [text[i : i + 5] for i in range(0, len(text), 5)], send, edit, delete
    )
    await editor("abcdefghijklmno")
    assert sent == ["abcde", "fghij", "klmno"]
    await editor("NEW", True)
    assert edited == [(1, "NEW")]
    assert deleted == [3, 2]
    assert len(editor.pages) == 1


def test_discord_pages_keep_code_fences_and_all_output():
    text = (
        "# Result\n\n```python\n"
        + "\n".join(f"print('line {i}')" for i in range(1000))
        + "\n```\n\nDone."
    )
    pages = discord_render(text)
    assert len(pages) > 1
    assert all(len(page) <= 2000 and page.count("```") % 2 == 0 for page in pages)
    assert sum(page.count("line 999") for page in pages) == 1
    assert "Done." in pages[-1]


async def test_answer_stream_coalesces_and_never_duplicates_final_text():
    edits = []
    live = asyncio.Event()

    async def edit(text, final=False):
        edits.append((text, final))
        if not final:
            live.set()

    stream = RemoteAnswerStream(edit, interval=0.02)
    stream.start()
    for text in ("Hello", " **world", "**"):
        stream.feed(ev.TextDelta(text))
    await asyncio.wait_for(live.wait(), timeout=5)
    stream.feed(ev.AssistantMessage(text="Hello **world**", agent_name="Nova", agent_color="cyan"))
    assert await stream.finalize("Hello **world**")
    assert edits == [("Hello **world**", False), ("Hello **world**", True)]
    assert stream.task is None


async def test_answer_retry_and_error_are_visible():
    calls = []
    retried = asyncio.Event()

    async def edit(text, final=False):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("rate limited")
        retried.set()

    stream = RemoteAnswerStream(edit, interval=0.01)
    stream.start()
    stream.feed(ev.TextDelta("Live answer"))
    await asyncio.wait_for(retried.wait(), timeout=5)
    assert len(calls) >= 2
    stream.feed(ev.Error(message="Upstream unavailable"))
    assert await stream.finalize()
    assert "Task failed" in calls[-1] and "Upstream unavailable" in calls[-1]


async def test_telegram_editors_are_independent_topic_aware_and_keep_every_page():
    bridge = TelegramBridge(BridgeConfig(RemotePlatform.TELEGRAM, "token", 123), asyncio.Queue())
    calls = []

    async def api(method, params, **kwargs):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(calls)}}

    bridge._api_call = api
    route = {"sid": "session-a", "thread": 42}
    status, answer = bridge._editor(123, route), bridge._editor(123, route)
    await status("**Working**\n\n> ✓ `read_file(a.py)`")
    await answer("```python\n" + "\n".join(f"line {i} <x>" for i in range(1000)) + "\n```", True)
    sends = [payload for method, payload in calls if method == "sendMessage"]
    assert len(sends) > 2
    assert all(
        payload["message_thread_id"] == 42 and payload["parse_mode"] == "HTML" for payload in sends
    )
    assert sum("line 999" in payload["text"] for payload in sends) == 1
    assert all(
        payload["text"].count("<pre>") == payload["text"].count("</pre>") for payload in sends
    )
    assert set(bridge._owner.values()) == {"session-a"}


async def test_manager_uses_only_connected_configured_bridges():
    manager = RemoteBridgeManager(asyncio.Queue())
    live = SimpleNamespace(is_connected=True, stream_target=AsyncMock(return_value="live"))
    offline = SimpleNamespace(is_connected=False, stream_target=AsyncMock())
    manager.bridges = lambda: [live, offline]
    assert await manager.stream_targets(sid="root") == ["live"]
    live.stream_target.assert_awaited_once_with(sid="root")
    offline.stream_target.assert_not_called()


async def test_enabling_remote_after_launch_starts_one_consumer():
    app = _app()
    async with app.run_test():
        app.session_state._remote_message_queue = asyncio.Queue()
        app._ensure_remote_consumer()
        worker = app._remote_consumer_worker
        app._ensure_remote_consumer()
        assert app._remote_consumer_worker is worker and not worker.is_finished
        worker.cancel()


async def test_local_tui_turn_streams_answer_and_tools_to_both_platforms(monkeypatch):
    app = _app()
    outputs = {"discord": [], "telegram": []}
    live = {key: asyncio.Event() for key in outputs}
    targets = []
    for platform in outputs:

        async def edit_status(text, final=False, key=platform):
            outputs[key].append(("status", text, final))

        async def edit_answer(text, final=False, key=platform):
            outputs[key].append(("answer", text, final))
            if not final:
                live[key].set()

        targets.append(
            SimpleNamespace(edit_fn=edit_status, answer_edit_fn=edit_answer, reply_fn=AsyncMock())
        )

    async def fake_stream(text, assistant_id=None):
        for _target, status, answer in app._local_remote_streams:
            answer.interval = 0.01
            status._interval = 0.01
        app._feed_remote(
            None, ev.ToolCall(name="read_file", display_str="read_file(a.py)", icon="", call_id="t")
        )
        app._feed_remote(
            None, ev.ToolResult(call_id="t", preview="OK", full_output="OK", is_error=False)
        )
        app._feed_remote(None, ev.TextDelta("**Answer**\n\nWorking result"))
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in live.values())), timeout=5
        )
        for records in outputs.values():
            assert any(kind == "answer" and not final for kind, _text, final in records)
        app._feed_remote(
            None,
            ev.AssistantMessage(
                text="**Answer**\n\nWorking result", agent_name="Nova", agent_color="cyan"
            ),
        )

    monkeypatch.setattr(app, "_do_stream", fake_stream)
    for name in (
        "_save_session",
        "_update_context_breakdown",
        "_check_context",
        "_drain_deferred_commands",
        "_drain_deferred_prompts",
    ):
        monkeypatch.setattr(app, name, AsyncMock())
    async with app.run_test():
        app.session_state._remote_bridge_manager = SimpleNamespace(
            stream_targets=AsyncMock(return_value=targets)
        )
        await app._stream_prompt("Inspect a.py")
        for records in outputs.values():
            assert any(
                kind == "status" and "read_file(a.py)" in text for kind, text, _final in records
            )
            assert sum(kind == "answer" and final for kind, _text, final in records) == 1
            assert any(
                kind == "answer" and final and text == "**Answer**\n\nWorking result"
                for kind, text, final in records
            )
        assert app._local_remote_streams == []


async def test_discord_status_and_answer_have_independent_pages_and_no_mentions():
    discord = pytest.importorskip("discord")
    from novacode_cli.remote.discord_bridge import DiscordBridge

    messages, sends = [], []

    async def send(body, **kwargs):
        assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)
        assert not kwargs["allowed_mentions"].everyone
        assert not kwargs["allowed_mentions"].users
        sends.append(body)
        message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        messages.append(message)
        return message

    bridge = DiscordBridge(BridgeConfig(RemotePlatform.DISCORD, "token", "123"), asyncio.Queue())
    channel = SimpleNamespace(send=send)
    status, answer = bridge._editor(channel), bridge._editor(channel)
    await status("**Working**\n\n> ✓ `read_file(a.py)`")
    await answer("```python\n" + "x = 1\n" * 700 + "```", True)
    assert len(messages) >= 4
    await answer("**Finished**", True)
    messages[1].edit.assert_awaited_once()
    assert all(message.delete.await_count == 1 for message in messages[2:])
    messages[0].edit.assert_not_called()


async def test_stopping_remote_blocks_captured_editors():
    from novacode_cli.remote.streaming import connected_editor

    bridge = SimpleNamespace(is_connected=True)
    underlying = AsyncMock()
    edit = connected_editor(bridge, underlying)
    await edit("Live")
    bridge.is_connected = False
    with pytest.raises(RuntimeError, match="stopped"):
        await edit("More output")
    assert underlying.await_count == 1


async def test_child_remote_turn_streams_without_duplicate_reply():
    app = _app()
    answer_edit, reply = AsyncMock(), AsyncMock()
    msg = SimpleNamespace(
        text="Review",
        user_name="me",
        edit_fn=AsyncMock(),
        answer_edit_fn=answer_edit,
        reply_fn=reply,
        react_fn=None,
    )
    async with app.run_test():
        pane = await _add_pane(app, sid="child-remote", title="reviewer")
        pane.child = object()
        app._session_supervisor = SimpleNamespace(send_prompt=AsyncMock(return_value="p1"))
        await app._remote_child_turn(pane, msg)
        app._feed_remote(pane, ev.TextDelta("Review result"))
        app._feed_remote(
            pane, ev.AssistantMessage(text="Review result", agent_name="Nova", agent_color="cyan")
        )
        await app._finish_remote_turn(pane)
        assert answer_edit.call_args.args == ("**[reviewer]** Review result", True)
        reply.assert_not_awaited()
        assert not pane.remote_turns


async def test_telegram_flood_control_delays_next_request_without_plain_fallback(monkeypatch):
    bridge = TelegramBridge(BridgeConfig(RemotePlatform.TELEGRAM, "token", 123), asyncio.Queue())
    responses = [
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 120},
        },
        {"ok": True, "result": {"message_id": 1}},
    ]
    requests = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return responses.pop(0)

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(bridge, "_ensure_session", lambda: SimpleNamespace(post=post))
    sleep = AsyncMock()
    monkeypatch.setattr("novacode_cli.remote.telegram_bridge.asyncio.sleep", sleep)
    assert await bridge._send_html("sendMessage", {"chat_id": 123}, "<b>Hi</b>") is None
    assert len(requests) == 1
    assert await bridge._send_html("sendMessage", {"chat_id": 123}, "<b>Hi</b>")
    assert sleep.await_args.args[0] > 119
    assert len(requests) == 2 and all(params["parse_mode"] == "HTML" for params in requests)


async def test_stream_buffer_stays_bounded_and_reports_truncation():
    edit = AsyncMock()
    stream = RemoteAnswerStream(edit)
    stream.feed(ev.TextDelta("x" * 300_000))
    assert len(stream.text) == 256_000
    assert await stream.finalize()
    assert "exceeded" in edit.await_args.args[0]


async def test_ctrl_b_agent_stream_is_isolated_from_foreground_and_finishes(monkeypatch):
    app = _app()
    answer_edit, status_edit = AsyncMock(), AsyncMock()
    target = SimpleNamespace(edit_fn=status_edit, answer_edit_fn=answer_edit, reply_fn=AsyncMock())

    async def fake_stream(*args, **kwargs):
        yield ev.ToolCall(name="read_file", display_str="read_file(bg.py)", icon="", call_id="bg")
        yield ev.ToolResult(call_id="bg", preview="OK", full_output="OK", is_error=False)
        yield ev.TextDelta("Background result")
        yield ev.AssistantMessage(text="Background result", agent_name="Nova", agent_color="cyan")
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", fake_stream)
    async with app.run_test():
        app.session_state._remote_bridge_manager = SimpleNamespace(
            stream_targets=AsyncMock(return_value=[target])
        )
        await app._bg_agent_worker("Inspect bg.py", 1).wait()
        assert answer_edit.await_args.args == ("Background result", True)
        assert "bg[1]" in status_edit.await_args.args[0]
        assert "read_file(bg.py)" in status_edit.await_args.args[0]
        target.reply_fn.assert_not_awaited()
        assert app._local_remote_streams == []
