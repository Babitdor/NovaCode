"""File context, skill activation, and all background task kinds reach the TUI."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from textual.widgets import OptionList

from novacode_cli.backends import OptimizedFilesystemBackend
from novacode_cli.commands.skill_invoke import SkillInvocation
from novacode_cli.core.input_preparation import prepare_input_content
from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware
from novacode_cli.skills.runtime import active_skill_names
from novacode_cli.tui.background_tasks import AgentTask
from novacode_cli.tui.screens import BackgroundTasksScreen
from novacode_cli.tui.widgets import PromptInput
from novacode_cli.ui_events import Done, Error
from tests.test_skills_runtime import RecordingModel
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


@pytest.mark.asyncio
async def test_delete_reference_contains_exact_file_and_contents(tmp_path, monkeypatch):
    path = tmp_path / "NAMI.md"
    path.write_text("File contents loaded", encoding="utf-8")
    monkeypatch.setattr("novacode_cli.input_utils._mention_roots", lambda: [tmp_path])
    content = await prepare_input_content("Remove the files @NAMI.md")
    assert str(path) in content
    assert "File contents loaded" in content
    assert "no filename search is needed" in content
    assert "Searching for dependencies" in content


@pytest.mark.asyncio
async def test_slash_skill_is_a_user_pin_and_activates_in_graph(tmp_path, monkeypatch):
    directory = tmp_path / "review"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\nInspect carefully."
    )
    monkeypatch.setattr("novacode_cli.skills.refreshing_middleware.prewarm", lambda: None)
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", lambda: set())
    middleware = RefreshingSkillsMiddleware(
        backend=OptimizedFilesystemBackend(root_dir=tmp_path, virtual_mode=True), sources=["/"]
    )
    graph = create_agent(RecordingModel(responses=[AIMessage("Done")]), middleware=[middleware])
    invocation = SkillInvocation(
        "unused", "review", "project", "Review", args="this diff", pinned_prompt="$review this diff"
    )
    monkeypatch.setattr(
        "novacode_cli.commands.skill_invoke._try_skill_invocation",
        AsyncMock(return_value=invocation),
    )
    app = _app()
    results = []

    async def stream(prompt):
        results.append(await graph.ainvoke({"messages": [HumanMessage(prompt)]}))

    monkeypatch.setattr(app, "_stream_prompt", stream)
    async with app.run_test():
        original = app._add_message
        displayed = AsyncMock(side_effect=original)
        monkeypatch.setattr(app, "_add_message", displayed)
        assert await app._run_skill("/review this diff")
        assert "pinned skill: review" in displayed.call_args.args[0].plain
        assert displayed.call_args.args[1] == "user"
        assert active_skill_names(results[0]["messages"], middleware._skills) == {"review"}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["done", "failed", "terminated"])
async def test_ctrl_b_agent_is_live_in_dialog_and_finishes(outcome, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def stream(*args, **kwargs):
        started.set()
        await release.wait()
        yield Error(message="provider failed") if outcome == "failed" else Done()

    monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", stream)
    app = _app()
    monkeypatch.setattr(app, "_active_agent", lambda: (object(), None))
    async with app.run_test() as pilot:
        app.query_one("#prompt", PromptInput).value = "Review files in the background"
        await pilot.press("ctrl+b")
        await asyncio.wait_for(started.wait(), 5)
        task = next(iter(app._bg_agent_tasks.values()))
        screen = BackgroundTasksScreen(
            extra_tasks=app._background_agent_tasks, clear_extra=app._clear_background_agents
        )
        app.push_screen(screen)
        await pilot.pause()
        assert task in screen._tasks
        assert screen.query_one("#tasks-list", OptionList).option_count >= 1
        assert task.status == "running"
        if outcome == "terminated":
            screen.query_one("#tasks-list", OptionList).highlighted = screen._tasks.index(task)
            screen.action_terminate()
        else:
            release.set()
        for _ in range(100):
            if task.worker.is_finished:
                break
            await asyncio.sleep(0.01)
        screen._refresh()
        assert task.status == outcome
        stopped_runtime = task.runtime()
        await asyncio.sleep(0.02)
        assert task.runtime() == stopped_runtime
        screen.action_clear()
        assert task.task_id not in app._bg_agent_tasks


@pytest.mark.asyncio
async def test_dialog_includes_remote_async_tasks(monkeypatch):
    app = _app()
    app._async_watcher = SimpleNamespace(
        running_tasks=lambda: [{"task_id": "remote-1", "agent_name": "reviewer", "runtime": 12}]
    )
    async with app.run_test() as pilot:
        screen = BackgroundTasksScreen(extra_tasks=app._background_agent_tasks)
        app.push_screen(screen)
        await pilot.pause()
        assert any(
            task.task_id == "async:remote-1" and task.command == "reviewer"
            for task in screen._tasks
        )
        app._async_watcher = None


def test_completed_agent_records_are_bounded():
    app = _app()
    for index in range(70):
        task = AgentTask(f"agent-{index}", "Review")
        task.finish("done")
        app._bg_agent_tasks[task.task_id] = task
    assert len(app._background_agent_tasks()) == 50
    assert len(app._bg_agent_tasks) == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 35), (60, 20), (40, 12)])
async def test_task_list_uses_available_height_and_scrolls(size):
    app = _app()
    tasks = [AgentTask(f"agent-{index}", f"Background task {index}") for index in range(20)]
    async with app.run_test(size=size) as pilot:
        screen = BackgroundTasksScreen(extra_tasks=lambda: tasks)
        app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#tasks-list", OptionList)
        assert ol.region.height >= max(3, size[1] // 2 - 2)
        assert ol.region.bottom <= size[1]
        assert ol.region.right <= size[0]
        assert ol.option_count >= 20
        ol.highlighted = ol.option_count - 1
        await pilot.pause()
        assert ol.scroll_y > 0
        scroll_y = ol.scroll_y
        selected_id = screen._selected().task_id
        screen._refresh()
        await pilot.pause()
        assert screen._selected().task_id == selected_id
        assert ol.scroll_y == scroll_y
