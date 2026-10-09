"""Task ownership, controls, and lifecycle updates across session tabs."""

import asyncio
from unittest.mock import Mock
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from novacode_cli.sessions.tasks import TabJobRegistry, job_snapshot
from novacode_cli.shell import jobs
from novacode_cli.tui.session_pane import fresh_state
from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401


def test_snapshots_keep_ids_local_and_bound_logs():
    registry = jobs.JobRegistry()
    job = registry.add("child command", "shell")
    registry.append_log(job.id, "x" * 50000)
    send = Mock()
    proxy = TabJobRegistry(send)
    proxy.update(job_snapshot(registry))
    assert proxy.active()[0].output == ""
    proxy.update(job_snapshot(registry, include_logs=True))
    assert len(proxy.resolve(job.task_id).output) == 20000
    assert proxy.terminate(job.task_id)
    send.assert_called_once_with("terminate", job.id)
    assert not job.kill.is_set(), "The parent must not act on its own same-numbered job"
    registry.mark_terminated(job.id)
    proxy.update(job_snapshot(registry))
    assert not proxy.terminate(job.id)
    assert not proxy.active()


@pytest.mark.asyncio
async def test_footer_and_hidden_root_notes_are_owned_by_tab(monkeypatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    app = _app()
    async with app.run_test() as pilot:
        child = await _add_pane(app)
        child.state = fresh_state()
        root_job = registry.add("ROOT ONLY", "shell")
        await pilot.pause()
        await app._switch_to(child)
        assert "active" not in app.query_one("#tasks-bar", Static).classes
        before = list(app._pending_job_notes)
        root_buffer = len(app._root_pane.buffer)
        registry.complete(root_job.id, 0)
        await pilot.pause()
        assert app._pending_job_notes == before
        app._report_bg_agent_done(1, "root work", "root result")
        app._notify_async_done("info", "Done", "[async_task_id=root-async]")
        assert app._pending_job_notes == before
        assert any("root result" in note for note in app._root_pane.state["_pending_job_notes"])
        assert any("root-async" in note for note in app._root_pane.state["_pending_job_notes"])
        assert len(app._root_pane.buffer) == root_buffer + 2
        assert any("completed" in note for note in app._root_pane.state["_pending_job_notes"])
        other = jobs.JobRegistry()
        child_job = other.add("CHILD ONLY", "shell")
        await app._on_child_message(child.sid, {"t": "jobs", "jobs": job_snapshot(other)})
        assert "active" in app.query_one("#tasks-bar", Static).classes
        assert app._tasks_registry().resolve(child_job.id).command == "CHILD ONLY"
        await app._switch_to(app._root_pane)
        assert "active" not in app.query_one("#tasks-bar", Static).classes
        assert app._tasks_registry().resolve(root_job.id).command == "ROOT ONLY"


@pytest.mark.asyncio
async def test_child_keys_do_not_kill_or_detach_root_commands(monkeypatch):
    app = _app()
    kill, detach = Mock(), Mock()
    monkeypatch.setattr(jobs, "request_kill", kill)
    monkeypatch.setattr(jobs, "request_detach", detach)
    async with app.run_test():
        child = await _add_pane(app)
        child.state = fresh_state()
        await app._switch_to(child)

        async def nothing(*args):
            pass

        monkeypatch.setattr(app._supervisor(), "cancel", nothing)
        monkeypatch.setattr(app, "_send_job_control", nothing)
        monkeypatch.setattr(app, "_open_tasks_panel", Mock())
        app.action_cancel_turn()
        await app.action_run_background()
        await asyncio.sleep(0)
        kill.assert_not_called()
        detach.assert_not_called()


@pytest.mark.asyncio
async def test_child_tasks_panel_controls_only_selected_worker(monkeypatch):
    from novacode_cli.tui.screens import BackgroundTasksScreen

    parent = jobs.JobRegistry()
    root_job = parent.add("ROOT", "shell")
    monkeypatch.setattr(jobs, "get_registry", lambda: parent)
    app = _app()
    sent = []

    async def control(pane, action, job_id):
        sent.append((pane.sid, action, job_id))

    monkeypatch.setattr(app, "_send_job_control", control)
    async with app.run_test() as pilot:
        pane = await _add_pane(app)
        pane.state = fresh_state()
        await app._switch_to(pane)
        registry = jobs.JobRegistry()
        child_job = registry.add("CHILD", "shell")
        await app._on_child_message(pane.sid, {"t": "jobs", "jobs": job_snapshot(registry)})
        await app._dispatch_to_child(pane, "/tasks")
        await pilot.pause()
        assert isinstance(app.screen, BackgroundTasksScreen)
        app.screen.query_one("#tasks-list").highlighted = 0
        await pilot.press("t")
        await pilot.pause()
        assert (pane.sid, "terminate", child_job.id) in sent
        assert not root_job.kill.is_set()
        await pilot.press("escape")
        await pilot.pause()
        await app._on_child_message(pane.sid, {"t": "exited", "crashed": True})
        assert not app._tasks_registry().active()
        assert "active" not in app.query_one("#tasks-bar", Static).classes


@pytest.mark.asyncio
async def test_child_logs_are_correlated_and_pending_requests_clear_on_exit(monkeypatch):
    app = _app()
    async with app.run_test():
        pane = await _add_pane(app)
        other = await _add_pane(app, "other")
        supervisor = app._supervisor()
        monkeypatch.setattr(supervisor, "get", lambda sid: SimpleNamespace(alive=True))

        async def send(child, message):
            assert message["job_id"] == "task_41"
            # Another tab's reply with the same request ID grants no response.
            await app._on_child_message(
                other.sid,
                {
                    "t": "job_logs",
                    "request_id": message["request_id"],
                    "output": "WRONG",
                },
            )
            assert not pane.job_log_waiters[message["request_id"]].done()
            await app._on_child_message(
                pane.sid,
                {
                    "t": "job_logs",
                    "request_id": message["request_id"],
                    "output": "RIGHT",
                },
            )
            return True

        monkeypatch.setattr(supervisor, "_send", send)
        assert await app._child_job_logs(pane, "task_41") == "RIGHT"
        assert not pane.job_log_waiters

        async def no_reply(child, message):
            return True

        monkeypatch.setattr(supervisor, "_send", no_reply)
        request = asyncio.create_task(app._child_job_logs(pane, "task_41"))
        await asyncio.sleep(0)
        await app._on_child_message(pane.sid, {"t": "exited"})
        assert await asyncio.wait_for(request, 0.5) is None
        assert not pane.job_log_waiters
