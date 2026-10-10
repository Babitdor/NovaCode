"""Streaming and teardown contracts without model/network calls."""

from __future__ import annotations

# Fakes mirror the dynamic UI/event interfaces.
# ruff: noqa: ANN001, ANN002, ANN003, ANN202
import asyncio
import io
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from rich.console import Console

from novacode_cli import events
from novacode_cli import ui_events as ev
from novacode_cli.tui.output_buffer import MAX_PENDING_CHARS
from novacode_cli.ui.stream_preview import ConsoleStreamPreview
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


def test_console_preview_first_delta_bound_and_terminal_restoration(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, force_interactive=True, width=60)
    preview = ConsoleStreamPreview(console)
    preview.append("first fragment")
    assert "first fragment" in output.getvalue()
    for _ in range(100):
        preview.append("x" * 2000)
    assert len(preview._tail) <= MAX_PENDING_CHARS
    assert preview._live is not None
    assert preview._live.refresh_per_second == 10
    preview.stop()
    preview.stop()
    assert preview._live is None
    assert not console._live_stack
    assert len(preview._tail) == 0
    assert "\x1b[?25h" in output.getvalue()


def test_redirected_console_keeps_final_message_contract():
    output = io.StringIO()
    preview = ConsoleStreamPreview(Console(file=output))
    preview.append("transient token")
    preview.stop()
    assert output.getvalue() == ""
    assert len(preview._tail) == 0


async def test_unmount_removes_observers_and_drops_late_tool_output(monkeypatch):
    app = _app()
    supervisor = SimpleNamespace(close_all=AsyncMock())
    app._session_supervisor = supervisor
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._on_tool_output in events._output_callbacks
        tasks, artifacts = app._task_registry, app._artifact_registry
        assert app._on_task_event_threadsafe in tasks._observers
        assert app._on_artifact_event_threadsafe in artifacts._observers
    assert app._on_tool_output not in events._output_callbacks
    assert app._on_task_event_threadsafe not in tasks._observers
    assert app._on_artifact_event_threadsafe not in artifacts._observers
    supervisor.close_all.assert_awaited_once_with(timeout=1.0)
    assert app._stall_watch._stop.is_set()
    post = Mock()
    monkeypatch.setattr(app, "_post_to_ui", post)
    app._on_tool_output("late", "output after unmount")
    assert not app._tool_out_pending
    post.assert_not_called()
    app._release_output_observers()


@pytest.mark.parametrize("replaced", [False, True])
def test_remote_status_callback_release_respects_new_owner(replaced):
    app = _app()
    callback = Mock()
    replacement = Mock() if replaced else callback
    manager = SimpleNamespace(_on_status=replacement, set_status_callback=Mock())
    app.session_state._remote_bridge_manager = manager
    app._remote_status_callback = callback
    app._release_output_observers()
    if replaced:
        manager.set_status_callback.assert_not_called()
    else:
        manager.set_status_callback.assert_called_once_with(None)


def test_gc_tuning_preserves_collectability(monkeypatch):
    from novacode_cli.tui import gc_tuning

    monkeypatch.setattr(gc_tuning, "sys", SimpleNamespace(modules={}))
    collect, freeze, thresholds = Mock(), Mock(), Mock()
    monkeypatch.setattr(gc_tuning.gc, "collect", collect)
    monkeypatch.setattr(gc_tuning.gc, "freeze", freeze)
    monkeypatch.setattr(gc_tuning.gc, "set_threshold", thresholds)
    assert gc_tuning.tune()
    freeze.assert_not_called()
    collect.assert_called_once()
    thresholds.assert_called_once_with(*gc_tuning.THRESHOLDS)


def test_watchdog_history_is_bounded(tmp_path):
    from novacode_cli.tui.stall_watch import StallWatch

    watcher = StallWatch(Mock(), log_path=tmp_path / "freeze.log")
    for _ in range(100):
        watcher._record(1.0, ["x" * 20_000] * 10)
    assert len(watcher.stalls) == 32
    assert all(len(samples) <= 4 for _, samples in watcher.stalls)
    assert sum(len(sample) for _, samples in watcher.stalls for sample in samples) <= 2 * 1024**2


@pytest.mark.parametrize("supplied", [False, True])
def test_worker_emitter_descriptor_ownership(monkeypatch, supplied):
    from novacode_cli.sessions.worker import _Emitter

    reader, writer = os.pipe()
    try:
        monkeypatch.setattr(os, "dup", lambda _fd: writer)
        emitter = _Emitter(writer if supplied else None)
        emitter.close()
        emitter.close()
        if supplied:
            os.fstat(writer)
        else:
            with pytest.raises(OSError, match=r"Bad file descriptor|handle is invalid"):
                os.fstat(writer)
    finally:
        os.close(reader)
        if supplied:
            os.close(writer)


def test_preview_skips_redraws_without_new_paint_interval(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    console = Console(file=io.StringIO(), force_terminal=True, force_interactive=True)
    monkeypatch.setattr("novacode_cli.ui.stream_preview.time.monotonic", lambda: 10.0)
    preview = ConsoleStreamPreview(console)
    preview.append("first")
    refresh = Mock()
    monkeypatch.setattr(preview._live, "refresh", refresh)
    for _ in range(100):
        preview.append("delta")
    refresh.assert_not_called()
    monkeypatch.setattr("novacode_cli.ui.stream_preview.time.monotonic", lambda: 10.2)
    preview.append("next")
    refresh.assert_called_once()
    preview.stop()


def test_loop_closing_race_closes_unsubmitted_coroutine(monkeypatch):
    app = _app()
    app._thread_id = -1
    app._loop = Mock()
    app._loop.is_closed.return_value = False
    captured = []

    def closed_loop(coroutine, _loop):
        captured.append(coroutine)
        message = "loop closed"
        raise RuntimeError(message)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", closed_loop)
    app._post_to_ui(Mock())
    assert captured[0].cr_frame is None


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_console_error_closes_stream_and_preview(monkeypatch, error):
    monkeypatch.setenv("TERM", "xterm")
    from novacode_cli.ui import execution
    from novacode_cli.vixie import server

    output = io.StringIO()
    console = Console(file=output, force_terminal=True, force_interactive=True)
    monkeypatch.setattr(execution, "console", console)
    for name in ("set_idle", "set_thinking", "set_working"):
        monkeypatch.setattr(server, name, AsyncMock())

    async def thinking():
        assert console._live_stack, "request feedback must precede ancillary I/O"

    monkeypatch.setattr(server, "set_thinking", thinking)
    closed = []

    async def source(*_args, **_kwargs):
        try:
            yield ev.TextDelta(text="live before error")
            raise error
        finally:
            closed.append(True)

    monkeypatch.setattr(execution, "run_with_goal", source)
    app = _app()
    if isinstance(error, RuntimeError):
        with pytest.raises(RuntimeError, match="failed"):
            await execution.execute_task("request", app.agent, None, app.session_state)
    else:
        await execution.execute_task("request", app.agent, None, app.session_state)
    assert closed == [True]
    assert not console._live_stack
    assert "live before error" in output.getvalue()
