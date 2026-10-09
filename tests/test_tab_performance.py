"""Work bounds and session isolation under background output and rapid switching."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from rich.text import Text
from textual.widgets import Static

from novacode_cli import ui_events as ev
from novacode_cli.sessions.supervisor import ChildSession, SessionSupervisor
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.session_pane import SessionPane, fresh_state
from novacode_cli.tui.session_tab import SessionTab
from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401


@pytest.mark.asyncio
async def test_background_token_burst_refreshes_by_transition_not_by_token(monkeypatch):
    app = _app()
    async with app.run_test():
        children = [await _add_pane(app, f"child-{i}") for i in range(3)]
        refresh = Mock()
        monkeypatch.setattr(app, "_refresh_tabs", refresh)
        for i in range(2000):
            pane = children[i % len(children)]
            await app._on_child_message(
                pane.sid, {"t": "ev", "c": "TextDelta", "d": {"text": "token"}}
            )
        assert refresh.call_count == 3
        assert all(pane.status == "running" and not pane.buffer for pane in children)
        await app._on_child_message(children[0].sid, {"t": "turn_done"})
        assert refresh.call_count == 4


def test_spinner_frames_do_not_trigger_layout_or_tab_relabel_messages(monkeypatch):
    tab = SessionTab(Text("1:◐ Project"))
    update = Mock()
    messages = Mock()
    monkeypatch.setattr(Static, "update", update)
    monkeypatch.setattr(tab, "post_message", messages)
    tab.label = Text("1:◓ Project")
    assert tab.label.plain == "1:◓ Project"
    assert update.call_args.kwargs == {"layout": False}
    messages.assert_not_called()
    update.reset_mock()
    tab.label = Text("1:◓ Project")
    update.assert_not_called()
    tab.label = Text("1:◓ Longer Project [TG]")
    messages.assert_called_once()
    assert isinstance(messages.call_args.args[0], SessionTab.Relabelled)


@pytest.mark.asyncio
async def test_switch_returns_before_history_and_rapid_switch_keeps_render_owner(monkeypatch):
    app = _app()
    rendered, owners = [], []

    async def render(event):
        owner = app._active_pane
        await asyncio.sleep(0.002)
        assert app._active_pane is owner, "An awaited render crossed into another session"
        owners.append(owner.sid)
        rendered.append(event.text)

    async with app.run_test() as pilot:
        first = await _add_pane(app, "one")
        second = await _add_pane(app, "two")
        first.state = fresh_state()
        second.state = fresh_state()
        monkeypatch.setattr(app, "_render", render)
        for i in range(100):
            await app._deliver(
                first, ev.AssistantMessage(text=str(i), agent_name="nova", agent_color="cyan")
            )
        await asyncio.wait_for(app._switch_to(first), 0.5)
        assert first.buffer  # The click handler did not wait for all 100 renders.
        await asyncio.sleep(0.01)
        switches = [
            asyncio.create_task(app._switch_to(pane)) for pane in (second, app._root_pane, second)
        ]
        await asyncio.wait_for(asyncio.gather(*switches), 0.5)
        assert app._active_pane is second
        assert first.buffer
        count = len(rendered)
        await pilot.pause()
        assert len(rendered) == count
        assert set(owners) == {first.sid}
        await app._switch_to(first)
        while first.sid in app._pane_replay_workers:
            await asyncio.sleep(0.01)
        assert rendered == [str(i) for i in range(100)]
        assert not first.buffer


@pytest.mark.asyncio
async def test_live_events_follow_buffered_history_without_overtaking(monkeypatch):
    app = _app()
    rendered = []

    async def render(event):
        rendered.append(event.text)
        await asyncio.sleep(0.001)

    async with app.run_test():
        child = await _add_pane(app)
        child.state = fresh_state()
        monkeypatch.setattr(app, "_render", render)
        for i in range(20):
            await app._deliver(
                child, ev.AssistantMessage(text=f"history-{i}", agent_name="n", agent_color="cyan")
            )
        await app._switch_to(child)
        await app._deliver(
            child, ev.AssistantMessage(text="live", agent_name="n", agent_color="cyan")
        )
        while child.sid in app._pane_replay_workers:
            await asyncio.sleep(0.01)
        assert rendered == [*(f"history-{i}" for i in range(20)), "live"]


def test_late_repaint_timer_does_not_clear_another_panes_latch():
    root = SimpleNamespace(state={})
    child = SimpleNamespace(state={})
    timers, paints = [], []
    app = SimpleNamespace(
        _active_pane=root,
        set_timer=lambda _, callback: timers.append(callback),
        _stream_flush_scheduled=True,
    )
    NovaApp._set_pane_timer(
        app, 0.1, lambda: paints.append(app._active_pane), "_stream_flush_scheduled"
    )
    app._active_pane = child
    timers[0]()
    assert root.state["_stream_flush_scheduled"] is False
    assert app._stream_flush_scheduled is True
    assert not paints


def test_saving_stream_state_does_not_join_or_copy_all_fragments():
    parts = ["x" * 1000 for _ in range(2000)]
    app = _app()
    app._live_buf_parts = parts
    pane = SessionPane(sid="test", title="test", scroll=None)
    pane.save_from(app)
    assert len(parts) == 2000
    assert pane.state["_live_buf_parts"] is parts
    other = fresh_state()
    assert other["_live_buf_parts"] == []
    assert other["_live_buf_parts"] is not other["_reasoning_buf_parts"]


def test_queued_preview_burst_cannot_evict_history_or_grow_without_bound():
    pane = SessionPane(sid="test", title="test", scroll=None)
    history = ev.AssistantMessage(text="keep history", agent_name="n", agent_color="cyan")
    pane.buffer.append(history)
    for _ in range(10000):
        NovaApp._queue_pane_event(pane, ev.TextDelta(text="token "))
    assert len(pane.buffer) == 2
    assert pane.buffer[0] is history
    assert len(pane.buffer[1].text) <= 20000
    NovaApp._queue_pane_event(pane, ev.StatusUpdate(message="old"))
    NovaApp._queue_pane_event(pane, ev.StatusUpdate(message="new"))
    assert len(pane.buffer) == 3
    assert pane.buffer[-1].message == "new"


@pytest.mark.asyncio
async def test_full_child_output_pipe_yields_before_reading_every_frame(monkeypatch):
    delivered, heartbeats = [], []

    class Stream:
        left = 1000

        async def readline(self):
            if not self.left:
                return b""
            self.left -= 1
            return b'{"t":"ev","c":"TextDelta","d":{"text":"token"}}\n'

    async def receive(sid, msg):
        delivered.append(msg)
        if len(delivered) == 1:
            asyncio.get_running_loop().call_soon(lambda: heartbeats.append(len(delivered)))

    supervisor = SessionSupervisor(receive)
    monkeypatch.setattr(supervisor, "_on_exit", AsyncMock())
    child = ChildSession(session_id="test", name="test", worktree=None)
    child.proc = SimpleNamespace(stdout=Stream())
    await supervisor._read_stdout(child)
    assert len(delivered) == 1000
    assert heartbeats and heartbeats[0] <= 32


@pytest.mark.asyncio
async def test_history_worker_cancellation_does_not_spawn_a_replacement(monkeypatch):
    from textual.worker import WorkerCancelled

    app = _app()
    entered = asyncio.Event()

    async def render(event):
        entered.set()
        await asyncio.Future()

    async with app.run_test():
        child = await _add_pane(app)
        child.state = fresh_state()
        monkeypatch.setattr(app, "_render", render)
        for i in range(3):
            await app._deliver(
                child, ev.AssistantMessage(text=str(i), agent_name="n", agent_color="cyan")
            )
        await app._switch_to(child)
        await asyncio.wait_for(entered.wait(), 2)
        worker = app._pane_replay_workers[child.sid]
        worker.cancel()
        with pytest.raises(WorkerCancelled):
            await worker.wait()
        await asyncio.sleep(0)
        assert child.sid not in app._pane_replay_workers
        assert len(child.buffer) == 2


@pytest.mark.asyncio
async def test_stale_tab_activation_cannot_undo_a_newer_selection(monkeypatch):
    from textual.widgets import Tabs

    app = _app()
    async with app.run_test() as pilot:
        child = await _add_pane(app)
        app._refresh_tabs()
        await pilot.pause()
        await app._switch_to(child)
        tabs = app.query_one("#session-tabs", Tabs)
        worker = Mock()
        monkeypatch.setattr(app, "run_worker", worker)
        app.on_tabs_tab_activated(Tabs.TabActivated(tabs, tabs.get_tab(app._root_pane.sid)))
        worker.assert_not_called()
        assert app._active_pane is child


@pytest.mark.asyncio
async def test_project_picker_disk_checks_do_not_block_the_event_loop(tmp_path, monkeypatch):
    import threading

    from novacode_cli import path_approval

    loop_thread = threading.get_ident()
    reads = []

    class Approval:
        def __init__(self):
            reads.append(threading.get_ident())

        def list_approved_paths(self):
            return {str(tmp_path): {}}

        def is_path_approved(self, path):
            reads.append(threading.get_ident())
            return True

    monkeypatch.setattr(path_approval, "PathApprovalManager", Approval)
    msg = SimpleNamespace(sender_id="42", chat_id="7", thread_id=88, reply_fn=AsyncMock())
    app = SimpleNamespace(
        _post_telegram_keyboard=AsyncMock(), _telegram_picker_key=NovaApp._telegram_picker_key
    )
    await NovaApp._request_telegram_session_launch(app, msg, "")
    assert reads and all(thread != loop_thread for thread in reads)
    app._post_telegram_keyboard.assert_awaited_once()
