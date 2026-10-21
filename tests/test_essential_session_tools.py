"""Scheduling persistence, feature gates and bounded inspection behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.memory import InMemoryStore

from novacode_cli.remote.scheduler import CronScheduler
from novacode_cli.tools.cron_tools import build_cron_tools
from novacode_cli.tools.inspection_tools import (
    build_inspection_tools,
    capture_terminal,
    inspect_context,
)


@pytest.fixture(autouse=True)
def isolate_gates(monkeypatch):
    for gate in (
        "CONTEXT_COLLAPSE",
        "TERMINAL_PANEL",
        "NOVA_CONTEXT_COLLAPSE",
        "NOVA_TERMINAL_PANEL",
    ):
        monkeypatch.delenv(gate, raising=False)


@pytest.mark.asyncio
async def test_cron_tools_share_session_queue_persist_resume_delete(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    session = SimpleNamespace(headless=False)
    tools = {t.name: t for t in build_cron_tools(session)}
    try:
        result = await tools["CronCreateTool"].ainvoke(
            {"cron_expr": "17 9 * * *", "task": "review the project"}
        )
        job_id = result["job_id"]
        listed = await tools["CronListTool"].ainvoke({})
        assert any(j["job_id"] == job_id for j in listed["jobs"])
        scheduler = session._cron_scheduler
        await scheduler._tick(datetime(2026, 10, 21, 9, 17))
        pending = []
        while not session._remote_message_queue.empty():
            pending.append(session._remote_message_queue.get_nowait())
        assert any(m.chat_id == job_id and m.text == "review the project" for m in pending)
        await scheduler.stop()

        session._cron_scheduler = None
        listed = await tools["CronListTool"].ainvoke({})
        assert any(j["job_id"] == job_id for j in listed["jobs"])
        assert (await tools["CronDeleteTool"].ainvoke({"job_id": job_id}))["deleted"]
        assert not (await tools["CronDeleteTool"].ainvoke({"job_id": job_id}))["deleted"]
        await session._cron_scheduler.stop()
        restored = CronScheduler(asyncio.Queue(), store=store)
        await restored._load_jobs()
        assert all(j["job_id"] != job_id for j in restored.list_jobs())
    finally:
        if getattr(session, "_cron_scheduler", None):
            await session._cron_scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"cron_expr": "bad", "task": "check"},
        {"cron_expr": "* * * * *", "task": "   "},
    ],
)
async def test_invalid_cron_inputs_do_not_start_scheduler(arguments):
    session = SimpleNamespace(headless=False)
    result = await build_cron_tools(session)[0].ainvoke(arguments)
    assert "error" in result
    assert not hasattr(session, "_cron_scheduler")


@pytest.mark.asyncio
async def test_headless_creation_reports_no_execution_consumer():
    tools = build_cron_tools(SimpleNamespace(headless=True))
    assert "error" in await tools[0].ainvoke({"cron_expr": "* * * * *", "task": "check"})


@pytest.mark.asyncio
async def test_headless_listing_and_deletion_do_not_start_a_scheduler(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    original = CronScheduler(asyncio.Queue(), store=store)
    job_id = await original.add_job("0 9 * * *", "saved")
    session = SimpleNamespace(headless=True)
    tools = {t.name: t for t in build_cron_tools(session)}
    result = await tools["CronListTool"].ainvoke({})
    assert not result["running"] and result["jobs"][0]["job_id"] == job_id
    assert (await tools["CronDeleteTool"].ainvoke({"job_id": job_id}))["deleted"]
    assert not hasattr(session, "_cron_scheduler")


@pytest.mark.asyncio
async def test_persistence_errors_never_report_success_or_lose_live_job(monkeypatch):
    store = InMemoryStore()
    scheduler = CronScheduler(asyncio.Queue(), store=store)
    job_id = await scheduler.add_job("* * * * *", "existing")

    async def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(store), "aput", fail)
    monkeypatch.setattr(type(store), "adelete", fail)
    with pytest.raises(OSError, match="disk full"):
        await scheduler.add_job("* * * * *", "new")
    with pytest.raises(OSError, match="disk full"):
        await scheduler.remove_job(job_id)
    assert [j["task"] for j in scheduler.list_jobs()] == ["existing"]


def test_gates_control_registration_and_recheck_execution(monkeypatch):
    assert build_inspection_tools() == []
    assert "error" in capture_terminal.invoke({"panel_id": "task_1"})
    assert "error" in inspect_context.func(SimpleNamespace(state={}))
    monkeypatch.setenv("CONTEXT_COLLAPSE", "1")
    assert [t.name for t in build_inspection_tools()] == ["CtxInspectTool"]
    monkeypatch.setenv("NOVA_TERMINAL_PANEL", "true")
    assert {t.name for t in build_inspection_tools()} == {"CtxInspectTool", "TerminalCaptureTool"}
    assert "runtime" not in inspect_context.tool_call_schema.model_json_schema()["properties"]


def test_context_inspection_is_content_free_and_does_not_mutate_state(monkeypatch):
    monkeypatch.setenv("CONTEXT_COLLAPSE", "1")
    state = {
        "messages": [HumanMessage(content="secret instruction"), AIMessage(content="answer")],
        "_context_snapshot": {
            "estimated_input_tokens": 700,
            "estimated_schema_tokens": 200,
            "message_content": "secret instruction",
        },
    }
    result = inspect_context.func(SimpleNamespace(state=state))
    assert result["message_count"] == 2
    assert result["messages_by_type"] == {"human": 1, "ai": 1}
    assert result["request_snapshot"]["estimated_input_tokens"] == 700
    assert "secret instruction" not in str(result)
    assert len(state["messages"]) == 2


def test_terminal_capture_bounds_tails_strips_ansi_and_handles_missing_panels(monkeypatch):
    from novacode_cli.shell import jobs

    monkeypatch.setenv("TERMINAL_PANEL", "1")
    registry = jobs.JobRegistry()
    monkeypatch.setattr(jobs, "get_registry", lambda: registry)
    job = registry.add("echo output", "shell")
    job.logs.append("first\n\x1b[31msecond\x1b[0m\n" + "x" * 500)
    result = capture_terminal.invoke({"panel_id": job.task_id, "tail_lines": 2, "max_chars": 128})
    assert result["truncated"] and len(result["text"]) == 128
    assert result["text"] == "x" * 128
    result = capture_terminal.invoke({"panel_id": job.task_id})
    assert "second" in result["text"] and "\x1b" not in result["text"]
    assert "error" in capture_terminal.invoke({"panel_id": "task_99999"})
    with pytest.raises(ValueError):
        capture_terminal.invoke({"panel_id": job.task_id, "max_chars": -1})


@pytest.mark.asyncio
async def test_reload_keeps_more_than_one_store_page():
    store = InMemoryStore()
    original = CronScheduler(asyncio.Queue(), store=store)
    for index in range(125):
        await original.add_job("0 9 * * *", f"job {index}")
    restored = CronScheduler(asyncio.Queue(), store=store)
    await restored._load_jobs()
    assert len(restored.list_jobs()) == 125


@pytest.mark.asyncio
async def test_jobs_survive_sqlite_close_reopen_and_deletion(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    from novacode_cli.memory.store import DualModeStore, _StdlibSqliteStore

    path = tmp_path / "jobs.db"
    with closing(
        sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    ) as connection:
        store = DualModeStore(_StdlibSqliteStore(connection))
        scheduler = CronScheduler(asyncio.Queue(), store=store)
        job_id = await scheduler.add_job("0 9 * * *", "persistent review")
    with closing(
        sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    ) as connection:
        store = DualModeStore(_StdlibSqliteStore(connection))
        monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
        tools = {t.name: t for t in build_cron_tools(SimpleNamespace(headless=True))}
        result = await tools["CronListTool"].ainvoke({})
        assert result["jobs"][0]["job_id"] == job_id
        assert (await tools["CronDeleteTool"].ainvoke({"job_id": job_id}))["deleted"]
    with closing(
        sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    ) as connection:
        store = DualModeStore(_StdlibSqliteStore(connection))
        scheduler = CronScheduler(asyncio.Queue(), store=store)
        await scheduler._load_jobs()
        assert scheduler.list_jobs() == []


@pytest.mark.asyncio
async def test_context_tool_receives_runtime_state_in_real_agent_graph(monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import ToolMessage

    monkeypatch.setenv("CONTEXT_COLLAPSE", "1")

    class Model(GenericFakeChatModel):
        def bind_tools(self, *args, **kwargs):
            return self

    model = Model(
        messages=iter(
            [
                AIMessage(
                    content="", tool_calls=[{"name": "CtxInspectTool", "args": {}, "id": "ctx"}]
                ),
                AIMessage(content="done"),
            ]
        )
    )
    graph = create_agent(model, tools=build_inspection_tools())
    result = await graph.ainvoke({"messages": [HumanMessage(content="inspect context")]})
    tool_result = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert tool_result.status == "success"
    assert '"message_count": 2' in tool_result.content
