"""/update's "Update now": save, exit, update in the terminal, restart into the session."""

from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from novacode_cli import updates


@pytest.fixture(autouse=True)
def clear_request(monkeypatch):
    monkeypatch.setattr(updates, "restart_request", None)
    # App mounts must not leave a live network task in the test executor.
    # Individual update-screen tests explicitly stub the manual check.
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    network = Mock(side_effect=AssertionError("Offline update test attempted a network request"))
    monkeypatch.setattr(updates, "_get_json", network)
    yield
    network.assert_not_called()


@pytest.mark.parametrize(("update_code", "relaunched"), [(0, True), (1, True), (130, False)])
def test_restart_updates_then_reopens_the_session(monkeypatch, update_code, relaunched):
    calls = []
    monkeypatch.setattr(updates, "restart_request", {"session": "s1", "continue": True})
    monkeypatch.setattr(
        updates, "_update_with_progress", lambda **kw: calls.append(("update", kw)) or update_code
    )
    monkeypatch.setattr(
        updates, "_relaunch", lambda session, **kw: calls.append(("relaunch", session, kw)) or 7
    )
    result = updates.restart_after_update()
    assert calls[0] == ("update", {"restarting": True})
    if relaunched:
        assert calls[1] == ("relaunch", "s1", {"continue_turn": True}), "a failed update still restarts"
        assert result == 7
    else:
        assert len(calls) == 1 and result == 130, "Ctrl+C during the update does not restart"


def test_relaunch_runs_this_environment_into_the_session_and_waits(monkeypatch):
    process = Mock()
    process.wait.side_effect = [KeyboardInterrupt, 3, 0]  # Ctrl+C belongs to the new Nova
    popen = Mock(return_value=process)
    monkeypatch.setattr(updates.os, "name", "nt")
    monkeypatch.setattr(updates.subprocess, "Popen", popen)
    assert updates._relaunch("s1", continue_turn=True) == 3
    command = popen.call_args.args[0]
    assert command == [sys.executable, "-m", "novacode_cli", "--continue", "s1"]
    assert popen.call_args.kwargs["env"][updates.CONTINUE_ENV] == "1"

    updates._relaunch(None, continue_turn=False)
    assert popen.call_args.args[0] == [sys.executable, "-m", "novacode_cli"]
    assert updates.CONTINUE_ENV not in popen.call_args.kwargs["env"]


def test_restarting_update_says_it_restarts(monkeypatch, capsys):
    monkeypatch.setattr(
        updates, "check_for_update", lambda **_kw: updates.UpdateStatus(True, "a", "b")
    )
    monkeypatch.setattr(updates, "detect_installation", lambda: updates.Installation("1.0", "uv-tool"))
    monkeypatch.setattr(updates, "install_update", lambda progress: True)
    monkeypatch.setattr(updates, "installed_version", lambda _python: "1.1")
    from novacode_cli import update_ui

    monkeypatch.setattr(update_ui.time, "sleep", lambda _s: None)
    assert updates._update_with_progress(restarting=True) == 0
    out = capsys.readouterr().out
    assert "Restarting Nova" in out and "Run nova to start" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_update_now_saves_stops_and_exits_with_a_restart_request(monkeypatch, running):
    from tests.test_tui_compaction_row import _app

    app = _app()
    order = []
    state = {"running": running}

    async def save_session(**_kw):
        order.append("save")

    async def stop():
        order.append("stop")
        state["running"] = False
        return True

    async def choose(_screen):
        order.append("asked")
        return 0

    async with app.run_test(size=(120, 40)):
        app.session_state.session_id = "s1"
        app.session_manager = Mock(load_session_meta=Mock(return_value=object()))
        monkeypatch.setattr(app, "_turn_running", lambda: state["running"])
        monkeypatch.setattr(app, "_stop_main_turn", stop)
        monkeypatch.setattr(app, "_save_session", save_session)
        monkeypatch.setattr(app, "push_screen_wait", choose)
        monkeypatch.setattr(app, "exit", lambda *a, **k: order.append("exit"))
        await app._update_and_restart()

    assert order == (["asked", "stop", "save", "exit"] if running else ["save", "exit"])
    assert updates.restart_request == {"session": "s1", "continue": running}


@pytest.mark.asyncio
async def test_cancelling_keeps_the_running_turn(monkeypatch):
    from tests.test_tui_compaction_row import _app

    app = _app()

    async def cancel(_screen):
        return 1

    async with app.run_test(size=(120, 40)):
        monkeypatch.setattr(app, "_turn_running", lambda: True)
        monkeypatch.setattr(app, "push_screen_wait", cancel)
        stop = Mock()
        monkeypatch.setattr(app, "_stop_main_turn", stop)
        await app._update_and_restart()
    stop.assert_not_called()
    assert updates.restart_request is None


@pytest.mark.asyncio
async def test_the_restarted_nova_continues_the_interrupted_turn(monkeypatch):
    from tests.test_tui_compaction_row import _app

    app = _app()
    sent = []
    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "_dispatch", sent.append)
        monkeypatch.setenv(updates.CONTINUE_ENV, "1")
        app._replay_history()
        await pilot.pause()
        app._replay_history()  # only once
        await pilot.pause()
    assert len(sent) == 1 and "Continue the task where you left off" in sent[0]
    assert updates.CONTINUE_ENV not in __import__("os").environ


@pytest.mark.asyncio
async def test_update_screen_offers_update_now_when_one_is_available(monkeypatch):
    from novacode_cli.tui.screens import UpdateScreen
    from tests.test_tui_compaction_row import _app
    from textual.widgets import Button

    monkeypatch.setattr(updates, "check_for_update", lambda **_kw: updates.UpdateStatus(True, "a" * 12, "b" * 12))
    monkeypatch.setattr(updates, "release_details", lambda status: status)
    app = _app()
    restart = Mock()
    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "_update_and_restart", restart)
        screen = UpdateScreen()
        app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        button = screen.query_one("#update-install", Button)
        assert not button.disabled
        monkeypatch.setattr(app, "run_worker", lambda coro, **_kw: None)
        screen.on_button_pressed(Button.Pressed(button))
        await pilot.pause()
    restart.assert_called_once()
