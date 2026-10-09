"""Telegram launch confirmations are tied to their requesting sender and topic."""

import time
import asyncio
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from novacode_cli.remote.bridge import RemotePlatform
from novacode_cli.tui.app import NovaApp


def _message(sender="42", chat="-100", thread=9):
    replies = []

    async def reply(text):
        replies.append(text)

    return SimpleNamespace(
        sender_id=sender,
        chat_id=chat,
        thread_id=thread,
        reply_fn=reply,
        replies=replies,
        message_id=123,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expired", [("Launch", False), ("Cancel", False), ("Launch", True)])
async def test_terminal_launch_actions_remove_keyboard_before_startup(
    action, expired, tmp_path, monkeypatch
):
    from novacode_cli import path_approval

    monkeypatch.setattr(
        path_approval,
        "PathApprovalManager",
        lambda: SimpleNamespace(is_path_approved=lambda _: True),
    )
    bot = SimpleNamespace(
        _config=SimpleNamespace(chat_id="-100"), remove_keyboard=AsyncMock(return_value=True)
    )
    requester = _message()
    requester.route = {"sid": "source"}

    async def spawn(*args, **kwargs):
        assert bot.remove_keyboard.await_count == 1
        assert not app._remote_launch_approvals
        return SimpleNamespace(sid="child", title="Example")

    app = SimpleNamespace(
        _remote_launch_approvals={
            "deadbeef1234": {
                "sender_id": "42",
                "chat_id": "-100",
                "thread_id": 9,
                "expires": time.monotonic() + (-1 if expired else 300),
                "request": {"folder": str(tmp_path), "name": "Example", "task": ""},
            }
        },
        _remote_telegram_bridges=lambda: [bot],
        spawn_session=AsyncMock(side_effect=spawn),
    )
    assert await NovaApp._handle_telegram_launch_message(app, requester, f"{action} deadbeef1234")
    bot.remove_keyboard.assert_awaited_once()
    assert bot.remove_keyboard.await_args.kwargs == {
        "thread_id": 9,
        "sid": "source",
        "reply_to_message_id": 123,
    }
    assert app.spawn_session.await_count == int(action == "Launch" and not expired)


@pytest.mark.asyncio
async def test_stale_launch_click_cannot_clear_a_new_picker():
    bot = SimpleNamespace(_config=SimpleNamespace(chat_id="-100"), remove_keyboard=AsyncMock())
    app = SimpleNamespace(
        _remote_launch_approvals={},
        _remote_telegram_bridges=lambda: [bot],
        _remote_project_pickers={("42", "-100", "9"): {}},
    )
    assert await NovaApp._handle_telegram_launch_message(app, _message(), "Launch deadbeef1234")
    bot.remove_keyboard.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_click_during_startup_is_consumed_without_second_launch(
    tmp_path, monkeypatch
):
    from novacode_cli import path_approval

    monkeypatch.setattr(
        path_approval,
        "PathApprovalManager",
        lambda: SimpleNamespace(is_path_approved=lambda _: True),
    )
    started, finish = asyncio.Event(), asyncio.Event()

    async def spawn(*args, **kwargs):
        started.set()
        await finish.wait()
        raise RuntimeError("startup failed")

    bot = SimpleNamespace(
        _config=SimpleNamespace(chat_id="-100"), remove_keyboard=AsyncMock(return_value=True)
    )
    app = SimpleNamespace(
        _remote_launch_approvals={
            "deadbeef1234": {
                "sender_id": "42",
                "chat_id": "-100",
                "thread_id": 9,
                "expires": time.monotonic() + 300,
                "request": {"folder": str(tmp_path), "name": "Example", "task": ""},
            }
        },
        _remote_telegram_bridges=lambda: [bot],
        spawn_session=AsyncMock(side_effect=spawn),
    )
    msg = _message()
    launching = asyncio.create_task(
        NovaApp._handle_telegram_launch_message(app, msg, "Launch deadbeef1234")
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert await NovaApp._handle_telegram_launch_message(app, msg, "Launch deadbeef1234")
        assert app.spawn_session.await_count == 1
        assert bot.remove_keyboard.await_count == 2
    finally:
        finish.set()
        await launching
    assert "startup failed" in msg.replies[-1]


@pytest.mark.asyncio
async def test_keyboard_cleanup_failure_falls_back_to_reply():
    bot = SimpleNamespace(
        _config=SimpleNamespace(chat_id="-100"),
        remove_keyboard=AsyncMock(side_effect=RuntimeError("offline")),
    )
    app = SimpleNamespace(_remote_telegram_bridges=lambda: [bot])
    msg = _message()
    await NovaApp._clear_telegram_keyboard(app, msg, "Cancelled")
    assert msg.replies == ["Cancelled"]


@pytest.mark.asyncio
async def test_idle_expiry_clears_keyboard_but_old_timer_cannot_clear_replacement():
    bot = SimpleNamespace(
        _config=SimpleNamespace(chat_id="-100"), remove_keyboard=AsyncMock(return_value=True)
    )
    records = {"request": {"expires": time.monotonic() + 300}}
    timers = []
    app = SimpleNamespace(
        is_running=True,
        set_timer=lambda delay, callback: timers.append(callback),
        _remote_telegram_bridges=lambda: [bot],
    )
    msg = _message()
    NovaApp._arm_telegram_picker_expiry(app, msg, records, "request")
    records["request"] = {"expires": time.monotonic() + 300}
    await timers[0]()
    bot.remove_keyboard.assert_not_awaited()
    NovaApp._arm_telegram_picker_expiry(app, msg, records, "request")
    await timers[1]()
    assert not records
    bot.remove_keyboard.assert_awaited_once()


def test_new_picker_invalidates_old_requests_only_in_same_sender_chat_topic():
    scoped = {"sender_id": "42", "chat_id": "-100", "thread_id": 9}
    app = SimpleNamespace(
        _remote_project_pickers={("42", "-100", "9"): {}, ("77", "-100", "9"): {}},
        _remote_tab_closers={("42", "-100", "9"): {}},
        _remote_launch_approvals={"old": scoped, "other": {**scoped, "thread_id": 10}},
    )
    NovaApp._reset_telegram_pickers(app, _message())
    assert not app._remote_tab_closers
    assert list(app._remote_launch_approvals) == ["other"]
    assert list(app._remote_project_pickers) == [("77", "-100", "9")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Can you start a new session in NovaCode project and investigate failing tests?",
            ("NovaCode", "investigate failing tests"),
        ),
        (
            "Open Project 2 in a new Nova session, connect Telegram, and investigate failing tests.",
            ("Project 2", "investigate failing tests"),
        ),
        ("Open Project 2 in a new Nova session.", ("Project 2", "")),
    ],
)
def test_natural_launch_parser_supports_project_before_or_after_session(text, expected):
    assert NovaApp._parse_natural_launch_intent(text) == expected


@pytest.mark.asyncio
async def test_wrong_sender_or_topic_cannot_approve_launch():
    app = SimpleNamespace(
        _remote_launch_approvals={
            "deadbeef1234": {
                "sender_id": "42",
                "chat_id": "-100",
                "thread_id": 9,
                "expires": time.monotonic() + 300,
                "request": {},
            }
        }
    )

    assert await NovaApp._handle_telegram_launch_message(
        app, _message(sender="77"), "Launch deadbeef1234"
    )
    assert await NovaApp._handle_telegram_launch_message(
        app, _message(thread=10), "Launch deadbeef1234"
    )
    assert "deadbeef1234" in app._remote_launch_approvals


@pytest.mark.asyncio
async def test_requester_can_cancel_and_expired_request_is_removed():
    app = SimpleNamespace(
        _remote_launch_approvals={
            "deadbeef1234": {
                "sender_id": "42",
                "chat_id": "-100",
                "thread_id": 9,
                "expires": time.monotonic() + 300,
                "request": {},
            },
            "cafefeed1234": {
                "sender_id": "42",
                "chat_id": "-100",
                "thread_id": 9,
                "expires": time.monotonic() - 1,
                "request": {},
            },
        }
    )
    requester = _message()

    assert await NovaApp._handle_telegram_launch_message(app, requester, "Cancel deadbeef1234")
    assert "Cancelled" in requester.replies[0]
    expired_from_other_user = _message(sender="77")
    assert await NovaApp._handle_telegram_launch_message(
        app, expired_from_other_user, "Launch cafefeed1234"
    )
    assert "cafefeed1234" in app._remote_launch_approvals
    expired = _message()
    assert await NovaApp._handle_telegram_launch_message(app, expired, "Launch cafefeed1234")
    assert "expired" in expired.replies[0]
    assert not app._remote_launch_approvals


@pytest.mark.asyncio
async def test_requester_can_approve_exactly_once_and_opens_tab(tmp_path, monkeypatch):
    from novacode_cli import path_approval

    class ApprovedPaths:
        def is_path_approved(self, _path):
            return True

    monkeypatch.setattr(path_approval, "PathApprovalManager", ApprovedPaths)
    pane = SimpleNamespace(title="NovaCode", sid="s-1234")
    calls = []

    async def spawn(name, task, *, directory, auto_approve=False):
        calls.append((name, task, directory))
        return pane

    app = SimpleNamespace(
        _remote_launch_approvals={
            "deadbeef1234": {
                "sender_id": "42",
                "chat_id": "-100123",
                "thread_id": 9,
                "expires": time.monotonic() + 300,
                "request": {"name": "NovaCode", "folder": str(tmp_path), "task": "inspect tests"},
            }
        },
        spawn_session=spawn,
        _remote_router=SimpleNamespace(topic_of=lambda _chat, _sid: 99),
    )
    requester = _message(chat="-100123")

    assert await NovaApp._handle_telegram_launch_message(app, requester, "Launch deadbeef1234")
    assert "Launch approved" in requester.replies[0]
    assert "Opened session tab" in requester.replies[1]
    assert "https://t.me/c/123/99" in requester.replies[1]
    assert calls == [("NovaCode", "inspect tests", tmp_path.resolve())]
    assert await NovaApp._handle_telegram_launch_message(app, requester, "Launch deadbeef1234")
    assert len(calls) == 1
    assert "already completed" in requester.replies[-1]


@pytest.mark.asyncio
async def test_natural_language_launch_extracts_approved_project_and_task():
    calls = []

    async def request(_msg, folder, *, task=""):
        calls.append((folder, task))

    async def no_pending(_msg, _text):
        return False

    app = SimpleNamespace(
        _remote_sessions=lambda: {"root": "main"},
        _request_telegram_session_launch=request,
        _handle_telegram_launch_message=no_pending,
        _handle_telegram_tab_close_selection=no_pending,
        _handle_telegram_project_selection=no_pending,
        _parse_natural_launch_intent=NovaApp._parse_natural_launch_intent,
    )
    msg = _message()
    msg.platform = RemotePlatform.TELEGRAM
    msg.text = "Can you start a new session in NovaCode project and investigate failing tests?"
    msg.route = {}

    assert await NovaApp._remote_route(app, msg)
    assert calls == [("NovaCode", "investigate failing tests")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "expected"), [("/tab", "create"), ("/tab close", "close")])
async def test_remote_tab_commands_open_the_correct_picker(text, expected):
    calls = []

    async def request_project(_msg, folder, *, task=""):
        calls.append(("create", folder, task))

    async def request_close(_msg):
        calls.append(("close",))

    async def no_pending(_msg, _text):
        return False

    app = SimpleNamespace(
        _remote_sessions=lambda: {"root": "main"},
        _request_telegram_session_launch=request_project,
        _request_telegram_tab_close=request_close,
        _handle_telegram_launch_message=no_pending,
        _handle_telegram_tab_close_selection=no_pending,
        _handle_telegram_project_selection=no_pending,
    )
    msg = _message()
    msg.platform = RemotePlatform.TELEGRAM
    msg.text = text
    msg.route = {}

    assert await NovaApp._remote_route(app, msg)
    assert calls == ([("create", "", "")] if expected == "create" else [("close",)])


@pytest.mark.asyncio
async def test_telegram_close_button_closes_selected_live_child_tab():
    pane = SimpleNamespace(sid="child-1", title="Example", kind="child", status="idle")
    closed = []

    async def close(target):
        closed.append(target)

    app = SimpleNamespace(
        _remote_tab_closers={
            ("42", "-100", "9"): {
                "choices": {"1. Example": "child-1"},
                "expires": time.monotonic() + 300,
            }
        },
        _telegram_picker_key=NovaApp._telegram_picker_key,
        _pane_for=lambda sid: pane if sid == pane.sid else None,
        _close_session=close,
    )
    msg = _message()

    assert await NovaApp._handle_telegram_tab_close_selection(app, msg, "1. Example")
    assert closed == [pane]
    assert not app._remote_tab_closers


@pytest.mark.asyncio
async def test_local_tabs_close_shows_picker_and_closes_selected_child(monkeypatch):
    from novacode_cli.tui import app as tui_app

    root = SimpleNamespace(sid="root", title="main", kind="root", status="idle")
    first = SimpleNamespace(sid="child-1", title="one", kind="child", status="idle", branch=None)
    second = SimpleNamespace(
        sid="child-2", title="two", kind="child", status="running", branch=None
    )
    closed = []

    class Picker:
        def __init__(self, title, options, hint):
            self.title, self.options, self.hint = title, options, hint

    monkeypatch.setattr(tui_app, "PickScreen", Picker)

    async def choose(screen):
        assert isinstance(screen, Picker)
        assert screen.title == "Close session tab"
        assert len(screen.options) == 2
        return 1

    async def close(pane):
        closed.append(pane)

    app = SimpleNamespace(
        _panes=[root, first, second],
        _active_pane=root,
        push_screen_wait=choose,
        _close_session=close,
    )

    await NovaApp._run_tabs_command(app, "/tabs close")

    assert closed == [second]


@pytest.mark.asyncio
async def test_remote_launch_request_keeps_initial_task(tmp_path, monkeypatch):
    from novacode_cli import path_approval

    class ApprovedPaths:
        def list_approved_paths(self):
            return {str(tmp_path): {"recursive": True}}

        def is_path_approved(self, _path):
            return True

    monkeypatch.setattr(path_approval, "PathApprovalManager", ApprovedPaths)
    keyboards = []

    async def post_keyboard(_msg, prompt, choices):
        keyboards.append((prompt, choices))

    app = SimpleNamespace(
        session_state=SimpleNamespace(auto_approve=False),
        _remote_launch_approvals={},
        _remote_project_pickers={},
        _telegram_picker_key=NovaApp._telegram_picker_key,
        _post_telegram_keyboard=post_keyboard,
        _remote_owner_state=lambda: SimpleNamespace(auto_approve=False),
    )
    msg = _message()

    await NovaApp._request_telegram_session_launch(
        app, msg, "NovaCode", task="investigate failing tests"
    )

    picker = next(iter(app._remote_project_pickers.values()))
    assert picker["task"] == "investigate failing tests"
    assert next(iter(picker["choices"].values())) == str(tmp_path)
    assert keyboards[0][1] == list(picker["choices"])


@pytest.mark.asyncio
async def test_project_button_selection_then_launch_button_creates_approval(tmp_path, monkeypatch):
    from novacode_cli import path_approval

    class ApprovedPaths:
        def is_path_approved(self, _path):
            return True

    monkeypatch.setattr(path_approval, "PathApprovalManager", ApprovedPaths)
    keyboards = []

    async def post_keyboard(_msg, prompt, choices):
        keyboards.append((prompt, choices))

    app = SimpleNamespace(
        session_state=SimpleNamespace(auto_approve=False),
        _remote_launch_approvals={},
        _telegram_picker_key=NovaApp._telegram_picker_key,
        _launch_telegram_metadata=lambda chat, thread: {
            "chat_id": str(chat),
            "token_sha256": "hash",
            "origin_thread_id": thread,
        },
        _post_telegram_keyboard=post_keyboard,
        _remote_owner_state=lambda: SimpleNamespace(auto_approve=False),
    )

    async def queue_approval(msg, folder, *, name, task):
        await NovaApp._queue_telegram_launch_approval(app, msg, folder, name=name, task=task)

    app._queue_telegram_launch_approval = queue_approval
    msg = _message()
    picker_key = NovaApp._telegram_picker_key(msg)
    app._remote_project_pickers = {
        picker_key: {
            "choices": {"1. Example": str(tmp_path)},
            "task": "run the tests",
            "name": None,
            "expires": time.monotonic() + 300,
        }
    }

    assert await NovaApp._handle_telegram_project_selection(app, msg, "1. Example")
    request_id, pending = next(iter(app._remote_launch_approvals.items()))
    assert pending["request"]["folder"] == str(tmp_path.resolve())
    assert pending["request"]["task"] == "run the tests"
    assert keyboards[0][1] == [f"Launch {request_id}", f"Cancel {request_id}"]


@pytest.mark.asyncio
async def test_project_picker_selection_is_scoped_to_requester_and_topic():
    app = SimpleNamespace(
        _remote_project_pickers={
            ("42", "-100", "9"): {
                "choices": {"1. Example": "C:/Example"},
                "task": "",
                "name": None,
                "expires": time.monotonic() + 300,
            }
        }
    )
    app._telegram_picker_key = NovaApp._telegram_picker_key

    assert not await NovaApp._handle_telegram_project_selection(
        app, _message(sender="77"), "1. Example"
    )
    assert not await NovaApp._handle_telegram_project_selection(
        app, _message(thread=10), "1. Example"
    )
    assert ("42", "-100", "9") in app._remote_project_pickers


@pytest.mark.asyncio
async def test_local_launch_requires_confirmation_then_opens_approved_folder_as_tab(
    tmp_path, monkeypatch
):
    from novacode_cli import path_approval
    from novacode_cli.tui import app as tui_app

    class ApprovedPaths:
        def is_path_approved(self, _path):
            return True

        def list_approved_paths(self):
            return {str(tmp_path): {"recursive": True}}

    class Confirmation:
        def __init__(self, title, body):
            self.title = title
            self.body = body

    class Picker:
        def __init__(self, projects, *, task, preferred_folder):
            self.projects = projects
            self.task = task
            self.preferred_folder = preferred_folder

    monkeypatch.setattr(path_approval, "PathApprovalManager", ApprovedPaths)
    monkeypatch.setattr(tui_app, "ConfirmModal", Confirmation)
    monkeypatch.setattr(tui_app, "ProjectSessionPicker", Picker)
    calls = []
    logs = []

    async def confirm(screen):
        if isinstance(screen, Picker):
            assert screen.projects == [(f"{tmp_path.name} — {tmp_path.parent}", str(tmp_path))]
            assert screen.task == "run tests"
            return {"folder": str(tmp_path), "task": screen.task}
        assert isinstance(screen, Confirmation)
        assert "Open this folder in a new session tab?" in str(screen.body)
        return True

    async def spawn(name, task, *, directory, auto_approve=False):
        calls.append((name, task, directory))
        return SimpleNamespace()

    app = SimpleNamespace(
        session_state=SimpleNamespace(auto_approve=True),
        model_name="test-model",
        _launch_telegram_metadata=lambda: None,
        push_screen_wait=confirm,
        spawn_session=spawn,
        _log=logs.append,
    )

    await NovaApp._launch_project_session(app, folder=str(tmp_path), task="run tests")

    assert calls == [(tmp_path.name, "run tests", tmp_path.resolve())]
