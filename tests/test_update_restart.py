"""/update's "Update now": save, exit, update in the terminal, restart into the session."""

from __future__ import annotations

import os
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
    monkeypatch.setattr(updates.sys, "platform", "linux")
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
    assert command == [sys.executable, "-P", "-m", "novacode_cli", "--continue", "s1"]
    assert popen.call_args.kwargs["env"][updates.CONTINUE_ENV] == "1"
    assert popen.call_args.kwargs["env"]["NOVA_UPDATE_HANDOFF_PID"] == str(updates.os.getpid())

    updates._relaunch(None, continue_turn=False)
    assert popen.call_args.args[0] == [sys.executable, "-P", "-m", "novacode_cli"]
    assert updates.CONTINUE_ENV not in popen.call_args.kwargs["env"]
    assert popen.call_args.kwargs["env"]["NOVA_UPDATE_HANDOFF_PID"] == str(updates.os.getpid())


def test_windows_restart_leaves_the_update_to_a_clean_interpreter(monkeypatch):
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates, "restart_request", {"session": "s1", "continue": True})
    handoff = Mock(return_value=True)
    monkeypatch.setattr(updates, "_hand_off_update", handoff)
    update = Mock()
    monkeypatch.setattr(updates, "_update_with_progress", update)

    assert updates.restart_after_update() == 0
    handoff.assert_called_once_with({"session": "s1", "continue": True})
    update.assert_not_called()  # the running process must not touch its own mapped files


def test_a_handoff_that_cannot_start_reports_failure(monkeypatch):
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates, "restart_request", {})
    monkeypatch.setattr(updates, "_hand_off_update", lambda _request: False)
    monkeypatch.setattr(updates, "_update_with_progress", Mock())

    assert updates.restart_after_update() == 1


def test_handoff_starts_a_clean_update_that_waits_for_this_process(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(updates.subprocess, "Popen", popen)
    monkeypatch.setenv(updates.CONTINUE_ENV, "leftover-from-an-earlier-restart")

    assert updates._hand_off_update({"session": "s1", "continue": True}) is True

    assert popen.call_args.args[0] == [sys.executable, "-P", "-m", "novacode_cli", "update"]
    env = popen.call_args.kwargs["env"]
    assert env[updates.CLEAN_UPDATE_ENV] == "1"
    assert env[updates.HANDOFF_PID_ENV] == str(os.getpid())
    assert env[updates.RESTART_SESSION_ENV] == "s1"
    assert env[updates.CONTINUE_ENV] == "1"


def test_handoff_clears_a_stale_continue_marker(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(updates.subprocess, "Popen", popen)
    monkeypatch.setenv(updates.CONTINUE_ENV, "1")

    updates._hand_off_update({"session": None, "continue": False})

    env = popen.call_args.kwargs["env"]
    assert updates.CONTINUE_ENV not in env
    assert updates.RESTART_SESSION_ENV not in env


def test_handoff_wait_waits_for_the_exited_process(monkeypatch):
    import ctypes

    kernel = Mock()
    kernel.OpenProcess.return_value = 123
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setenv(updates.HANDOFF_PID_ENV, "4242")

    updates._wait_for_handoff()

    kernel.OpenProcess.assert_called_once()
    kernel.WaitForSingleObject.assert_called_once_with(123, 60_000)
    kernel.CloseHandle.assert_called_once_with(123)
    assert updates.HANDOFF_PID_ENV not in os.environ


def test_handoff_wait_without_a_parent_is_a_no_op(monkeypatch):
    monkeypatch.setenv(updates.HANDOFF_PID_ENV, "")

    updates._wait_for_handoff()

    assert updates.HANDOFF_PID_ENV not in os.environ


@pytest.mark.parametrize(("update_code", "relaunched"), [(0, True), (1, False), (130, False)])
def test_the_clean_updater_waits_then_updates_then_restarts(
    monkeypatch, capsys, update_code, relaunched
):
    calls = []
    monkeypatch.setenv(updates.CLEAN_UPDATE_ENV, "1")
    monkeypatch.setenv(updates.RESTART_SESSION_ENV, "s1")
    monkeypatch.setenv(updates.CONTINUE_ENV, "1")
    monkeypatch.setattr(updates, "_wait_for_handoff", lambda: calls.append("wait"))
    monkeypatch.setattr(
        updates, "_update_with_progress", lambda **kw: calls.append(("update", kw)) or update_code
    )
    monkeypatch.setattr(
        updates, "_relaunch", lambda session, **kw: calls.append(("relaunch", session, kw)) or 7
    )

    result = updates.update_main([])

    assert calls[0] == "wait", "the loader of the old process must be gone before uv runs"
    assert calls[1] == ("update", {"restarting": True})
    assert updates.CLEAN_UPDATE_ENV not in os.environ
    assert updates.RESTART_SESSION_ENV not in os.environ
    if relaunched:
        assert calls[2] == ("relaunch", "s1", {"continue_turn": True})
        assert result == 7
    else:
        assert len(calls) == 2
        assert result == update_code
        if update_code == 1:
            assert "Nova was not restarted" in capsys.readouterr().err


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
        await screen.check_update().wait()
        button = screen.query_one("#update-install", Button)
        assert not button.disabled
        monkeypatch.setattr(app, "run_worker", lambda coro, **_kw: None)
        screen.on_button_pressed(Button.Pressed(button))
        await pilot.pause()
    restart.assert_called_once()
