"""One Telegram chat, several Nova sessions: each message reaches the right one."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from novacode_cli.remote.routing import ROOT, RemoteRouter

SESSIONS = {ROOT: "main", "s-1": "alpha", "s-2": "beta"}


def _msg(text, *, chat=7, thread=None, reply_owner=None):
    return SimpleNamespace(text=text, chat_id=chat, thread_id=thread, reply_to_owner=reply_owner)


# ── routing precedence ─────────────────────────────────────────────────────


def test_plain_messages_go_to_main() -> None:
    route = RemoteRouter().resolve(_msg("fix the bug"), SESSIONS)
    assert (route.sid, route.text, route.via) == (ROOT, "fix the bug", "default")


def test_an_address_wins_and_is_stripped() -> None:
    router = RemoteRouter(topics={(7, "s-2"): 55})
    route = router.resolve(_msg("@Alpha run the tests", thread=55), SESSIONS)
    assert (route.sid, route.text, route.via) == ("s-1", "run the tests", "address")
    assert router.resolve(_msg("@nobody hi"), SESSIONS).sid == ROOT, "unknown names are text"


def test_a_topic_goes_to_its_session() -> None:
    router = RemoteRouter(topics={(7, "s-2"): 55})
    assert router.resolve(_msg("status?", thread=55), SESSIONS).sid == "s-2"
    assert router.resolve(_msg("status?", thread=99), SESSIONS) is None, "closed session"


def test_replying_continues_with_that_session() -> None:
    route = RemoteRouter().resolve(_msg("and the docs", reply_owner="s-2"), SESSIONS)
    assert (route.sid, route.via) == ("s-2", "reply")


def test_use_sets_the_default_and_forgets_closed_sessions() -> None:
    router = RemoteRouter()
    router.default[7] = router.match("beta", SESSIONS)
    assert router.resolve(_msg("go"), SESSIONS).sid == "s-2"
    router.topics[(7, "s-2")] = 55
    assert router.forget("s-2") == [(7, 55)]
    assert router.resolve(_msg("go"), SESSIONS).sid == ROOT
    assert router.match("main", SESSIONS) == ROOT


# ── Telegram bridge: topics and reply ownership ───────────────────────────


def _bridge():
    from novacode_cli.remote.bridge import BridgeConfig, RemotePlatform
    from novacode_cli.remote.telegram_bridge import TelegramBridge

    return TelegramBridge(
        BridgeConfig(platform=RemotePlatform.TELEGRAM, token="t", chat_id=7), asyncio.Queue()
    )


def test_bridge_parses_topic_and_reply_owner_and_remembers_senders() -> None:
    async def run() -> None:
        bridge = _bridge()
        calls: list[tuple[str, dict]] = []
        next_id = iter(range(100, 200))
        updates = [
            [
                {"update_id": 1, "message": {
                    "message_id": 1, "chat": {"id": 7}, "from": {"username": "me"},
                    "text": "in a topic", "message_thread_id": 55, "is_topic_message": True,
                }},
                {"update_id": 2, "message": {
                    "message_id": 2, "chat": {"id": 7}, "from": {"username": "me"},
                    "text": "a reply", "reply_to_message": {"message_id": 42},
                    # plain-group reply chains also carry a thread id: not a topic
                    "message_thread_id": 42,
                }},
            ]
        ]

        async def api(method, payload, **_):
            calls.append((method, payload))
            if method == "getMe":
                return {"ok": True, "result": {"username": "nova_bot"}}
            return {"ok": True, "result": {"message_id": next(next_id)}}

        async def get_updates():
            if updates:
                return updates.pop()
            bridge._running = False
            return []

        bridge._api_call = api
        bridge._get_updates = get_updates
        bridge._owner[42] = "s-2"
        await bridge.run()

        topic_msg, reply_msg = bridge._queue.get_nowait(), bridge._queue.get_nowait()
        assert topic_msg.thread_id == 55 and topic_msg.reply_to_owner is None
        assert reply_msg.thread_id is None and reply_msg.reply_to_owner == "s-2"

        topic_msg.route["sid"] = "s-1"
        await topic_msg.reply_fn("done")
        method, payload = calls[-1]
        assert method == "sendMessage" and payload["message_thread_id"] == 55
        assert "s-1" in bridge._owner.values(), "replying to it will reach s-1"

    asyncio.run(run())


# ── TUI: a message for a child session reaches it and is answered ─────────

pytest.importorskip("textual")


def test_child_session_turn_round_trip() -> None:
    from textual.containers import VerticalScroll

    from novacode_cli import ui_events as ev
    from novacode_cli.tui.session_pane import SessionPane, fresh_state
    from tests.test_tui_app import _SS, _FakeAgent

    async def run() -> None:
        from novacode_cli.tui.app import NovaApp
        from novacode_cli.ui.ui_elements import TokenTracker

        app = NovaApp(
            agent=_FakeAgent(), assistant_id="nova-agent", session_state=_SS(), backend=None,
            token_tracker=TokenTracker(), image_tracker=None, model_name="m",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            prompts: list[tuple[str, str]] = []

            class _Sup:
                async def send_prompt(self, sid, text):
                    prompts.append((sid, text))
                    return "p1"

            app._session_supervisor = _Sup()
            child_state = SimpleNamespace(auto_approve=False)
            pane = SessionPane(sid="s-1", title="alpha", scroll=VerticalScroll(), kind="child")
            pane.child = object()
            pane.state = fresh_state(session_state=child_state)
            app._panes.append(pane)

            replies: list[str] = []
            edits: list[str] = []

            async def reply_fn(text):
                replies.append(text)

            async def edit_fn(text, final=False):  # noqa: ARG001
                edits.append(text)

            msg = SimpleNamespace(
                text="@alpha run the tests", chat_id=7, thread_id=None, reply_to_owner=None,
                user_name="me", reply_fn=reply_fn, edit_fn=edit_fn, react_fn=None, route={},
            )
            assert await app._remote_route(msg) is True
            assert prompts == [("s-1", "run the tests")] and msg.route["sid"] == "s-1"
            assert child_state.auto_approve is True, "remote turns auto-approve"

            await app._deliver(pane, ev.ToolCall(name="shell", display_str='shell("pytest")',
                                                 icon="", call_id="c1"))
            await app._deliver(pane, ev.AssistantMessage(text="All 12 tests pass.",
                                                         agent_name="nova", agent_color="c"))
            await app._on_child_message("s-1", {"t": "turn_done"})

            assert replies[-1] == "**[alpha]** All 12 tests pass."
            assert any("shell" in e and "alpha" in e for e in edits), "labelled live status"
            assert child_state.auto_approve is False, "restored after the turn"

            plain = SimpleNamespace(text="hello", chat_id=7, thread_id=None,
                                    reply_to_owner=None, route={})
            assert await app._remote_route(plain) is False and plain.route["sid"] == ROOT

    asyncio.run(run())
