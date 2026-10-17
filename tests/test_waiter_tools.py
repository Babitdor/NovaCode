"""Bounded waits preserve completion results and release their UI indicators."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from novacode_cli.shell import jobs
from novacode_cli.tools import waiter_tools


@pytest.fixture(autouse=True)
def _no_released_waits():
    waiter_tools._released.clear()
    yield
    waiter_tools._released.clear()


async def test_user_input_releases_only_its_session_wait(monkeypatch: pytest.MonkeyPatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("long build", "shell")
    runtime = SimpleNamespace(
        state={}, tool_call_id="wait-input", config={"configurable": {"thread_id": "chat-1"}},
    )
    waiting = asyncio.create_task(
        waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=60)
    )
    try:
        for _ in range(20):
            if waiter_tools.active_waiters():
                break
            await asyncio.sleep(0)
        waiter_tools.interrupt_task_waits("other-chat")
        await asyncio.sleep(0)
        assert not waiting.done()
        waiter_tools.interrupt_task_waits("chat-1")
        result = await asyncio.wait_for(waiting, 1)
        assert "user input" in result.lower()
        assert job.status == "running"
        assert not job.kill.is_set()
        assert waiter_tools.active_waiters() == []
        assert registry._observers == []
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_shell_wait_returns_completion_and_shows_target(monkeypatch: pytest.MonkeyPatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("python build.py", "shell")
    runtime = SimpleNamespace(state={}, tool_call_id="call-1")

    waiting = asyncio.create_task(
        waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=2)
    )
    await asyncio.sleep(0)
    assert [(item.kind, item.target) for item in waiter_tools.active_waiters()] == [
        ("shell command", job.task_id)
    ]
    assert job.task_id in waiter_tools.list_waiters.invoke({})
    registry.complete(job.id, 0, "build passed\n")
    result = await waiting
    assert "done" in result
    assert "exit 0" in result
    assert "build passed" in result
    assert waiter_tools.active_waiters() == []


async def test_tui_prompt_releases_wait_and_delivers_guidance(monkeypatch: pytest.MonkeyPatch):
    from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401

    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("long build", "shell")
    app = _app()
    app.session_state.steering_instructions = []
    runtime = SimpleNamespace(
        state={}, tool_call_id="tui-wait",
        config={"configurable": {"thread_id": app.session_state.thread_id}},
    )
    async with app.run_test() as pilot:
        app._turn_active = True
        waiting = asyncio.create_task(
            waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=60)
        )
        try:
            await pilot.pause()
            assert waiter_tools.active_waiters()
            app.on_input_submitted(SimpleNamespace(
                input=app.query_one("#prompt"), value="Explain what the build is doing",
            ))
            result = await asyncio.wait_for(waiting, 1)
            assert "user input" in result.lower()
            assert app._live_steers[-1].instruction == "Explain what the build is doing"
            assert job.status == "running"
            assert waiter_tools.active_waiters() == []
        finally:
            app._turn_active = False
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_new_input_cancels_remote_observation_without_stopping_run(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.agents.default_subagents import async_subagents

    monkeypatch.setattr(
        async_subagents, "retrieve_async_subagents", lambda: [{"name": "reviewer", "url": "local"}],
    )
    checking = asyncio.Event()
    released = asyncio.Event()

    class Runs:
        async def get(self, **_kwargs: object) -> dict[str, str]:
            checking.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
            return {"status": "running"}

    monkeypatch.setattr(waiter_tools, "_remote_client", lambda _spec: SimpleNamespace(runs=Runs()))
    task = {
        "task_id": "remote-id", "thread_id": "remote-id", "run_id": "run-id",
        "agent_name": "reviewer", "status": "running",
    }
    runtime = SimpleNamespace(
        state={"async_tasks": {"remote-id": task}}, tool_call_id="remote-wait",
        config={"configurable": {"thread_id": "chat-remote"}},
    )
    waiting = asyncio.create_task(waiter_tools.wait_for_task.coroutine("remote-id", runtime))
    try:
        await asyncio.wait_for(checking.wait(), 1)
        waiter_tools.interrupt_task_waits("chat-remote")
        result = await asyncio.wait_for(waiting, 1)
        assert result.update["async_tasks"]["remote-id"]["status"] == "running"
        assert '"wait_interrupted": true' in result.update["messages"][0].content
        assert released.is_set()
        assert waiter_tools.active_waiters() == []
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_shell_wait_times_out_without_stopping_job(monkeypatch: pytest.MonkeyPatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("server", "shell")
    runtime = SimpleNamespace(state={}, tool_call_id="call-2")

    result = await waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=1)
    assert "running" in result
    assert job.status == "running"
    assert not job.kill.is_set()
    assert waiter_tools.active_waiters() == []


async def test_cancelling_wait_releases_indicator_and_observer(monkeypatch: pytest.MonkeyPatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("long build", "shell")
    runtime = SimpleNamespace(state={}, tool_call_id="call-cancel")
    waiting = asyncio.create_task(
        waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=60)
    )
    await asyncio.sleep(0)
    assert len(waiter_tools.active_waiters()) == 1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert waiter_tools.active_waiters() == []
    assert registry._observers == []
    assert job.status == "running"


async def test_finished_and_unknown_shell_jobs_return_immediately(monkeypatch: pytest.MonkeyPatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("test", "shell")
    registry.complete(job.id, 2, "failed assertion\n")
    runtime = SimpleNamespace(state={}, tool_call_id="call-3")

    result = await waiter_tools.wait_for_task.coroutine(job.task_id, runtime)
    missing = await waiter_tools.wait_for_task.coroutine("task_9999", runtime)
    assert "failed" in result
    assert "exit 2" in result
    assert "failed assertion" in result
    assert "No background shell task" in missing
    assert waiter_tools.active_waiters() == []


async def test_remote_wait_gets_result_and_updates_task_state(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.agents.default_subagents import async_subagents

    monkeypatch.setattr(
        async_subagents, "retrieve_async_subagents", lambda: [{"name": "reviewer", "url": "local"}]
    )

    class Runs:
        calls = 0

        async def get(self, **_kwargs: object) -> dict[str, str]:
            self.calls += 1
            return {"status": "running" if self.calls == 1 else "success"}

    class Threads:
        async def get(self, **_kwargs: object) -> dict[str, object]:
            return {"values": {"messages": [{"content": "review complete"}]}}

    client = SimpleNamespace(runs=Runs(), threads=Threads())
    monkeypatch.setattr(waiter_tools, "_remote_client", lambda _spec: client)
    monkeypatch.setattr(waiter_tools, "POLL_SECONDS", 0.01)
    task = {
        "task_id": "remote-id",
        "thread_id": "remote-id",
        "run_id": "run-id",
        "agent_name": "reviewer",
        "status": "running",
        "last_updated_at": "old",
    }
    runtime = SimpleNamespace(state={"async_tasks": {"remote-id": task}}, tool_call_id="call-4")

    result = await waiter_tools.wait_for_task.coroutine("remote-id", runtime, timeout_seconds=2)
    update = result.update
    assert update["async_tasks"]["remote-id"]["status"] == "success"
    assert "review complete" in update["messages"][0].content
    assert client.runs.calls == 2
    assert waiter_tools.active_waiters() == []


async def test_remote_wait_timeout_keeps_task_running_and_releases_waiter(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.agents.default_subagents import async_subagents

    monkeypatch.setattr(
        async_subagents, "retrieve_async_subagents", lambda: [{"name": "reviewer", "url": "local"}]
    )

    class Runs:
        async def get(self, **_kwargs: object) -> dict[str, str]:
            return {"status": "running"}

    monkeypatch.setattr(waiter_tools, "_remote_client", lambda _spec: SimpleNamespace(runs=Runs()))
    task = {
        "task_id": "remote-id",
        "thread_id": "remote-id",
        "run_id": "run-id",
        "agent_name": "reviewer",
        "status": "running",
    }
    runtime = SimpleNamespace(state={"async_tasks": {"remote-id": task}}, tool_call_id="call-5")

    result = await waiter_tools.wait_for_task.coroutine("remote-id", runtime, timeout_seconds=1)
    assert '"timed_out": true' in result.update["messages"][0].content
    assert result.update["async_tasks"]["remote-id"]["status"] == "running"
    assert waiter_tools.active_waiters() == []


async def test_remote_status_request_is_bounded(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.agents.default_subagents import async_subagents

    monkeypatch.setattr(
        async_subagents,
        "retrieve_async_subagents",
        lambda: [{"name": "reviewer", "url": "local"}],
    )

    class SlowRuns:
        async def get(self, **_kwargs: object) -> dict[str, str]:
            await asyncio.sleep(10)
            return {"status": "running"}

    monkeypatch.setattr(
        waiter_tools, "_remote_client", lambda _spec: SimpleNamespace(runs=SlowRuns())
    )
    task = {
        "task_id": "slow-id",
        "thread_id": "slow-id",
        "run_id": "run-id",
        "agent_name": "reviewer",
        "status": "running",
    }
    runtime = SimpleNamespace(state={"async_tasks": {"slow-id": task}}, tool_call_id="slow-call")

    result = await waiter_tools.wait_for_task.coroutine("slow-id", runtime, timeout_seconds=1)
    assert '"timed_out": true' in result.update["messages"][0].content
    assert waiter_tools.active_waiters() == []


async def test_tui_indicator_shows_waiter_count_and_target():
    from textual.widgets import Static

    from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401

    app = _app()
    async with app.run_test():
        bar = app.query_one("#tasks-bar", Static)
        with waiter_tools._tracking("task_42", "shell command"):
            assert "active" in bar.classes
            shown = bar.render().plain
            assert "waiting for task_42 0s of 10s" in shown, "the wait's own clock"
            assert "type to interrupt" in shown, "and the way out"
            assert "shell command: task_42" in str(bar.tooltip)
        assert "active" not in bar.classes


async def test_scripted_agent_can_call_waiter_with_injected_runtime(
    monkeypatch: pytest.MonkeyPatch,
):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    class Model(GenericFakeChatModel):
        def bind_tools(self, *_args: object, **_kwargs: object) -> Model:
            return self

    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("test command", "shell")
    registry.complete(job.id, 0, "passed\n")
    model = Model(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "wait-call",
                            "name": "wait_for_task",
                            "args": {"task_id": job.task_id},
                        }
                    ],
                ),
                AIMessage(content="The command passed."),
            ]
        )
    )
    graph = create_agent(model, tools=[waiter_tools.wait_for_task])

    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="Wait for the command")]},
        config={"configurable": {"thread_id": "scripted-wait"}},
    )
    contents = [str(message.content) for message in result["messages"]]
    assert any("done" in content and "passed" in content for content in contents)
    assert contents[-1] == "The command passed."


# -- the agent answers the user instead of waiting on and on -------------------


def _waits(n: int) -> list:
    """The user's message, then n steps that each called wait_for_task."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    history = [HumanMessage("run the build in the background")]
    for i in range(n):
        call = {"id": f"w{i}", "name": "wait_for_task", "args": {}, "type": "tool_call"}
        history += [AIMessage("", tool_calls=[call]), ToolMessage("running", tool_call_id=f"w{i}")]
    return history


def _runtime(messages: list, thread: str = "c") -> SimpleNamespace:
    return SimpleNamespace(
        state={"messages": messages}, tool_call_id="w", config={"configurable": {"thread_id": thread}}
    )


async def test_a_second_waiting_step_is_refused_with_the_status(monkeypatch: pytest.MonkeyPatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("cargo build", "shell")
    result = await asyncio.wait_for(
        waiter_tools.wait_for_task.coroutine(job.task_id, _runtime(_waits(2)), timeout_seconds=60), 2
    )
    assert job.task_id in result and "running" in result
    assert "end your turn" in result and "resumed automatically" in result
    assert job.status == "running" and not job.kill.is_set(), "the task itself keeps going"


def test_a_wait_is_a_short_look():
    assert waiter_tools._bounded_timeout(60) == 10 and waiter_tools._bounded_timeout(0) == 1


async def test_one_wait_is_allowed(monkeypatch: pytest.MonkeyPatch):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("cargo build", "shell")
    waiting = asyncio.create_task(
        waiter_tools.wait_for_task.coroutine(job.task_id, _runtime(_waits(1)), timeout_seconds=5)
    )
    await asyncio.sleep(0.05)
    registry.complete(job.id, 0, "ok\n")
    assert "exit 0" in await asyncio.wait_for(waiting, 2)


async def test_after_the_user_releases_a_wait_the_agent_must_reply_first(
    monkeypatch: pytest.MonkeyPatch,
):
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("cargo build", "shell")
    runtime = _runtime(_waits(1), thread="chat-9")
    waiting = asyncio.create_task(
        waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=60)
    )
    for _ in range(20):
        if waiter_tools.active_waiters():
            break
        await asyncio.sleep(0)
    waiter_tools.interrupt_task_waits("chat-9")
    assert "user input" in (await asyncio.wait_for(waiting, 1)).lower()
    again = await asyncio.wait_for(
        waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=60), 1
    )
    assert "reply to it first" in again, "refused at once instead of waiting another minute"
    waiting = asyncio.create_task(
        waiter_tools.wait_for_task.coroutine(job.task_id, runtime, timeout_seconds=5)
    )
    await asyncio.sleep(0.05)
    registry.complete(job.id, 0, "ok\n")
    assert "exit 0" in await asyncio.wait_for(waiting, 2), "after replying, waiting works again"
