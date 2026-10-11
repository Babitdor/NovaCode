"""Saved tabs restore isolated history and use the normal continuation startup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.sessions.resume import resolve_saved_tab
from novacode_cli.sessions.supervisor import SessionSupervisor
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


@pytest.fixture
def saved_session(tmp_path):
    project = tmp_path / "Prøject With Spaces"
    project.mkdir()
    manager = SessionManager(tmp_path / "sessions")
    manager.save_session(
        "saved-12345678",
        "old-thread",
        [HumanMessage("Question"), AIMessage("## Answer")],
        "saved-agent",
        project_root=project,
        model_name="saved-model",
        model_provider="ollama",
        todos=[{"content": "Continue work", "status": "pending"}],
    )
    return manager, project


def test_resolve_saved_tab_exact_and_unique_prefix(saved_session):
    manager, project = saved_session
    assert resolve_saved_tab(manager, "saved-12345678").project_root == str(project)
    assert resolve_saved_tab(manager, "saved-123").session_id == "saved-12345678"


@pytest.mark.parametrize("selector", ["../saved", "C:\\saved", "", "unknown"])
def test_resolve_rejects_paths_and_missing_ids(saved_session, selector):
    manager, project = saved_session
    with pytest.raises(ValueError):
        resolve_saved_tab(manager, selector)


def test_ambiguous_prefix_never_selects_arbitrarily(saved_session):
    manager, project = saved_session
    manager.save_session(
        "saved-12399999", "other-thread", [HumanMessage("Other")], "nova", project_root=project
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_saved_tab(manager, "saved-123")


def test_worker_argv_separates_transport_id_and_saved_identity():
    supervisor = SessionSupervisor(AsyncMock())
    fresh = supervisor._argv("s-new", "saved-agent")
    resumed = supervisor._argv("s-new", "saved-agent", "saved-12345678")
    assert "--continue" not in fresh
    assert resumed == fresh + ["--continue", "saved-12345678"]
    assert resumed[resumed.index("--session-id") + 1] == "s-new"


def test_session_without_saved_project_is_rejected(saved_session):
    manager, project = saved_session
    manager.save_session("no-project", "thread", [HumanMessage("Q")], "nova")
    with pytest.raises(ValueError, match="no saved project"):
        resolve_saved_tab(manager, "no-project")


def prepare_app(monkeypatch, manager, *, approved=True):
    from novacode_cli.path_approval import PathApprovalManager

    monkeypatch.setattr(PathApprovalManager, "is_path_approved", lambda *args: approved)
    app = _app()
    app.session_manager = manager
    app.session_state.auto_approve = False
    supervisor = SimpleNamespace(
        at_capacity=lambda: False,
        spawn=AsyncMock(return_value=object()),
        close_all=AsyncMock(),
        send_prompt=AsyncMock(),
    )
    app._session_supervisor = supervisor
    monkeypatch.setattr(app, "_open_remote_topics", AsyncMock())
    return app, supervisor


async def test_resume_opens_saved_project_history_without_replacing_root(
    monkeypatch, saved_session
):
    from novacode_cli.tui.session_transcript import SessionTranscript

    manager, project = saved_session
    app, supervisor = prepare_app(monkeypatch, manager)
    root_agent, root_state = app.agent, app.session_state
    async with app.run_test() as pilot:
        await app._run_tabs_command("/tabs resume saved-123")
        pane = app._active_pane
        assert pane.kind == "child" and pane.worktree == project.resolve()
        assert pane.sid != "saved-12345678"
        assert app.session_state.session_id == "saved-12345678"
        assert app.session_state.auto_approve is False
        assert app.model_name == "saved-model" and app.assistant_id == "saved-agent"
        history = pane.scroll.query_one(SessionTranscript)
        assert [message.content for message in history.session.messages] == [
            "Question",
            "## Answer",
        ]
        assert supervisor.spawn.call_args.kwargs["resume_id"] == "saved-12345678"
        assert supervisor.spawn.call_args.kwargs["worktree"] == project.resolve()
        supervisor.send_prompt.assert_not_called()
        await app._on_child_message(
            pane.sid,
            {
                "t": "ready",
                "session_id": "saved-12345678",
                "thread_id": "fresh-thread",
                "model": "actual-model",
                "model_provider": "ollama",
                "todos": [{"content": "Continue work", "status": "pending"}],
            },
        )
        assert app.session_state.thread_id == "fresh-thread"
        assert app.model_name == "actual-model"
        assert app._todos[0]["content"] == "Continue work"
        await app._switch_to(app._root_pane)
        assert app.agent is root_agent and app.session_state is root_state
        await app._run_tabs_command("/tabs resume saved-12345678")
        assert app._active_pane is pane
        assert supervisor.spawn.await_count == 1


@pytest.mark.parametrize("choice", [None, 0])
async def test_picker_cancellation_and_selection(monkeypatch, saved_session, choice):
    manager, project = saved_session
    app, supervisor = prepare_app(monkeypatch, manager)
    monkeypatch.setattr(app, "push_screen_wait", AsyncMock(return_value=choice))
    async with app.run_test():
        await app._run_tabs_command("/tabs resume")
        assert supervisor.spawn.await_count == (0 if choice is None else 1)
        assert len(app._panes) == (1 if choice is None else 2)


@pytest.mark.parametrize("failure", ["unapproved", "missing"])
async def test_invalid_project_does_not_open_or_spawn(monkeypatch, saved_session, failure):
    manager, project = saved_session
    if failure == "missing":
        project.rmdir()
    app, supervisor = prepare_app(monkeypatch, manager, approved=failure != "unapproved")
    async with app.run_test():
        await app._run_tabs_command("/tabs resume saved-123")
        supervisor.spawn.assert_not_called()
        assert app._active_pane is app._root_pane and len(app._panes) == 1


async def test_concurrent_resume_reserves_saved_identity(monkeypatch, saved_session):
    manager, project = saved_session
    app, supervisor = prepare_app(monkeypatch, manager)
    entered, release = asyncio.Event(), asyncio.Event()

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(app, "spawn_session", spawn)
    async with app.run_test():
        first = asyncio.create_task(app._resume_tab("saved-123"))
        await entered.wait()
        await app._resume_tab("saved-123")
        assert app._resuming_session_ids == {"saved-12345678"}
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not app._resuming_session_ids


async def test_turn_persists_before_completion_and_during_cancellation(monkeypatch):
    from tests.test_session_worker import _make

    worker, sent = _make(monkeypatch, [])
    save = AsyncMock(side_effect=lambda **kwargs: sent.append({"t": "saved"}))
    monkeypatch.setattr(worker, "_save", save)
    await worker._run_turn("p1", "question")
    assert [frame["t"] for frame in sent[-2:]] == ["saved", "turn_done"]

    entered = asyncio.Event()

    async def stream(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream)
    task = asyncio.create_task(worker._run_turn("p2", "question"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert save.await_count == 2
    assert [frame["t"] for frame in sent[-2:]] == ["saved", "turn_done"]
    assert not sent[-1]["ok"]
    save.assert_awaited_with(is_error=True)


async def test_cancelled_child_launch_removes_incomplete_tab(monkeypatch, saved_session):
    manager, project = saved_session
    app, supervisor = prepare_app(monkeypatch, manager)
    entered = asyncio.Event()

    async def spawn(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    supervisor.spawn.side_effect = spawn
    async with app.run_test():
        task = asyncio.create_task(app._resume_tab("saved-123"))
        await entered.wait()
        assert len(app._panes) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(app._panes) == 1 and app._active_pane is app._root_pane
        assert not app._resuming_session_ids
