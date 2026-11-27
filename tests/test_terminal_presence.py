"""Cross-terminal presence expires and never grants local pane access."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from novacode_cli.tui.command_center import CommandCenterScreen, pane_activity
from novacode_cli.tui.presence import TTL, TerminalPresence, read_presence
from tests.test_command_center import Host, Manager, meta
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


def test_presence_refresh_expiry_cleanup_and_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = [100.0]
    monkeypatch.setattr("novacode_cli.tui.presence.time.time", lambda: clock[0])
    first, second = TerminalPresence(tmp_path), TerminalPresence(tmp_path)
    first.publish([{"session_id": "first", "status": "Working"}])
    second.publish([{"session_id": "second", "status": "Ready"}])
    assert len(read_presence(tmp_path)) == 2
    first.path.parent.joinpath("broken.json").write_text("{", encoding="utf-8")
    clock[0] += TTL + 1
    assert read_presence(tmp_path) == []
    first.publish([{"session_id": "first", "status": "Needs you"}])
    assert read_presence(tmp_path)[0]["status"] == "Needs you"
    first.close()
    first.publish([{"session_id": "resurrected"}])
    assert read_presence(tmp_path) == []


async def test_status_ignores_stale_hidden_child_state_and_resolved_question():
    child = SimpleNamespace(
        kind="child", status="idle", pending_interrupt=None, state={"_turn_active": True}
    )
    app = SimpleNamespace(_active_pane=None)
    assert pane_activity(app, child) == "idle"
    child.status = "running"
    assert pane_activity(app, child) == "running"
    child.pending_interrupt = {}
    assert pane_activity(app, child) == "needs-approval"
    child.status = "crashed"
    assert pane_activity(app, child) == "crashed"
    child.status, child.pending_interrupt = "idle", None
    app._active_pane = child
    app._question_future = asyncio.get_running_loop().create_future()
    assert pane_activity(app, child) == "needs-approval"
    app._question_future.set_result("answered")
    assert pane_activity(app, child) == "idle"


async def test_center_observes_another_terminal_transition(tmp_path: Path):
    import time

    presence = TerminalPresence(tmp_path)
    manager = Manager([meta("remote")])
    manager.sessions_dir = tmp_path
    screen = CommandCenterScreen(manager, list)
    host = Host(screen)
    presence.publish([{"session_id": "remote", "status": "Working", "pane_id": "root"}])
    async with host.run_test() as pilot:
        # _load_presence is an exclusive worker the 1s interval also fires, so a
        # manual call can legitimately cancel a predecessor on a slow runner.
        # Converge on the file state instead of assuming one worker survives.
        async def observed_status(session_id: str, expected: str) -> bool:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                row = screen._rows.get(f"session:{session_id}")
                if row is not None and row.status == expected:
                    return True
                screen._load_presence()
                await pilot.pause(0.05)
            return False

        assert await observed_status("remote", "Working")
        row = screen._rows["session:remote"]
        assert row.external
        assert not row.pane_id
        await pilot.press("enter")
        assert host.result is None
        presence.publish([{"session_id": "remote", "status": "Needs you"}])
        assert await observed_status("remote", "Needs you")
        presence.close()
        assert await observed_status("remote", "Inactive")


async def test_app_publishes_transitions_and_removes_its_snapshot(
    tmp_path: Path, isolated_session_config: object  # noqa: F811, ARG001
):
    from novacode_cli.session.session_persistence import SessionManager

    app = _app()
    app.session_manager = SessionManager(tmp_path)
    app.session_state.session_id = "local"
    async with app.run_test():
        await app._publish_terminal_presence()
        assert read_presence(tmp_path)[0]["status"] == "Ready"
        app._turn_active = True
        await app._publish_terminal_presence()
        assert read_presence(tmp_path)[0]["status"] == "Working"
        app._question_future = asyncio.get_running_loop().create_future()
        await app._publish_terminal_presence()
        assert read_presence(tmp_path)[0]["status"] == "Needs you"
        app._question_future.set_result("answered")
        app._turn_active = False
        await app._publish_terminal_presence()
        assert read_presence(tmp_path)[0]["status"] == "Ready"
    assert read_presence(tmp_path) == []
