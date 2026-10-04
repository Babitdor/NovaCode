"""A question asked over the remote link disappears once it is answered.

Left in the chat, an answered question reads as still open — and on a busy
thread the next answer may be typed against the wrong one. The bridge hands the
TUI a way to take the question back; the TUI uses it as soon as the answer
arrives, or if the turn is cancelled while the question is pending.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "tests")


def _bridge():
    from novacode_cli.remote.bridge import BridgeConfig, RemotePlatform
    from novacode_cli.remote.telegram_bridge import TelegramBridge

    return TelegramBridge(
        BridgeConfig(platform=RemotePlatform.TELEGRAM, token="t", chat_id=7), asyncio.Queue()
    )


# ── the bridge ───────────────────────────────────────────────────────────────


def test_send_message_reports_the_ids_it_sent_and_delete_removes_them() -> None:
    async def run() -> None:
        bridge = _bridge()
        calls: list[tuple[str, dict]] = []
        ids = iter(range(500, 600))

        async def api(method, params=None):  # noqa: ANN001, ANN202
            calls.append((method, dict(params or {})))
            return {"ok": True, "result": {"message_id": next(ids)}}

        bridge._api_call = api
        sent = await bridge._send_message(7, "**Question:** which one?", sid="root")
        assert sent == [500]
        assert bridge._owner[500] == "root"

        await bridge._delete_messages(7, sent)
        assert ("deleteMessage", {"chat_id": 7, "message_id": 500}) in calls
        assert 500 not in bridge._owner, "a deleted message can no longer be replied to"

    asyncio.run(run())


def test_a_failed_delete_is_swallowed() -> None:
    """Telegram refuses deletes after 48 h; that must not break the turn."""

    async def run() -> None:
        bridge = _bridge()

        async def api(method, params=None):  # noqa: ANN001, ANN202
            raise RuntimeError("message can't be deleted")

        bridge._api_call = api
        await bridge._delete_messages(7, [1, 2])  # does not raise

    asyncio.run(run())


# ── the TUI side ─────────────────────────────────────────────────────────────


async def _ask(*, ask_fails: bool = False, cancel: bool = False, answer: str = "2"):
    import test_tui_app as T

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    app = NovaApp(
        agent=T._FakeAgent(), assistant_id="nova-agent", session_state=T._SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=None,
    )
    log = {"asked": [], "retracted": 0}

    async def ask_fn(text):  # noqa: ANN001, ANN202
        if ask_fails:
            raise RuntimeError("network down")
        log["asked"].append(text)

        async def retract() -> None:
            log["retracted"] += 1

        return retract

    async def reply_fn(text):  # noqa: ANN001, ANN202
        log["asked"].append(text)

    result = None
    async with app.run_test(size=(120, 40)) as pilot:
        app._remote_msg = SimpleNamespace(ask_fn=ask_fn, reply_fn=reply_fn)
        task = asyncio.ensure_future(
            app._ask_remote_question({"question": "Which db?", "options": ["sqlite", "postgres"]})
        )
        await pilot.pause()
        if ask_fails:
            result = await asyncio.wait_for(task, 2)
        elif cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            app._remote_question_future.set_result(SimpleNamespace(text=answer))
            result = await asyncio.wait_for(task, 2)
    return result, log


def test_the_question_is_deleted_once_answered() -> None:
    pytest.importorskip("textual")
    result, log = asyncio.run(_ask(answer="2"))
    assert result["response"]["answer"] == "postgres" and result["response"]["selected_index"] == 1
    assert log["retracted"] == 1, "the question must disappear after the answer"
    assert "**Question:** Which db?" in log["asked"][0], "bold needs real markdown"


def test_the_question_is_deleted_when_the_turn_is_cancelled() -> None:
    """A question nobody can answer any more must not be left hanging in chat."""
    pytest.importorskip("textual")
    _, log = asyncio.run(_ask(cancel=True))
    assert log["retracted"] == 1


def test_an_unsent_question_does_not_hang_the_turn() -> None:
    """The user never saw it, so no answer is coming."""
    pytest.importorskip("textual")
    result, log = asyncio.run(_ask(ask_fails=True))
    assert result["response"]["answer"] == ""
    assert log["retracted"] == 0
