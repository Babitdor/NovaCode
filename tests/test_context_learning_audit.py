"""Model-free acceptance cases for the context and learning audit."""

import os
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.store.memory import InMemoryStore

from novacode_cli.agents.context_compaction import user_messages
from novacode_cli.hermes.tracker import AtomicCounter, ToolUsageTracker
from novacode_cli.memory.agent_memory import AgentMemoryMiddleware
import asyncio


@pytest.mark.asyncio
async def test_budget_adapter_does_not_share_an_inherited_mutable_budget():
    from novacode_cli.context._budget import get_context_budget, reset_context_budget

    reset_context_budget()
    parent = get_context_budget()

    async def child():
        budget = get_context_budget()
        budget.track_middleware("task", "x" * 80)
        return budget

    first, second = await asyncio.gather(child(), child())
    assert first is not parent and second is not parent and first is not second
    assert parent.total_tokens == 0


def test_multimodal_blocks_survive_repeated_checkpoints():
    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware, limits_for

    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    request = HumanMessage(content=[{"type": "text", "text": "Use this exact diagram"}, block])
    middleware = ContextCompactionMiddleware(context_window=4000)
    first = middleware._summary_message([request], "summary", None, limits_for(4000))
    second = middleware._summary_message([first], "second summary", None, limits_for(4000))
    assert block in second.content
    assert second.additional_kwargs["user_content_blocks"] == request.content


def test_pending_approval_tool_arguments_remain_verbatim():
    from novacode_cli.agents.context_compaction import tail_start, trim_old_arguments

    call = AIMessage(
        content="",
        tool_calls=[
            {"id": "pending", "name": "edit_file", "args": {"content": "approved text " * 1000}}
        ],
    )
    messages = [HumanMessage("task"), call, HumanMessage("live steering")]
    cutoff = tail_start(messages, 1)
    assert cutoff == 1
    assert trim_old_arguments(messages, cutoff)[1].tool_calls == call.tool_calls


@pytest.mark.asyncio
async def test_automatic_compaction_discards_live_steering_changes():
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware

    request = ModelRequest(
        model=FakeListChatModel(responses=["unused"]),
        tools=[],
        state={},
        messages=[HumanMessage("task"), AIMessage("old " * 5000), HumanMessage("latest")],
    )
    middleware = ContextCompactionMiddleware(context_window=4000)
    plan = middleware._plan(request, force=True)

    async def summarize(*args):
        request.messages.append(HumanMessage("Live constraint: do not delete files"))
        return "prepared from obsolete source"

    middleware._summarize = summarize
    messages, update = await middleware._compact(request, plan, overflow=False)
    assert update is None
    assert messages[-1].content == "Live constraint: do not delete files"


def test_compaction_preserves_middle_constraints_and_all_steering():
    task = "start " * 1000 + "NEVER delete the database" + " end" * 1000
    steering = [f"constraint {i}" for i in range(10)]
    assert user_messages([HumanMessage(task), *map(HumanMessage, steering)], 20) == [
        task,
        *steering,
    ]


@pytest.mark.asyncio
async def test_independent_counters_do_not_share_state():
    store = InMemoryStore()
    first, second = AtomicCounter(store), AtomicCounter(store)
    await first.increment()
    assert await second.get() == 0
    await second.reset()
    assert await first.get() == 1


@pytest.mark.asyncio
async def test_independent_episode_windows_do_not_share_state():
    store = InMemoryStore()
    first, second = ToolUsageTracker(store), ToolUsageTracker(store)
    await first.record_tool_usage("edit_file", True)
    assert await second.get_tool_history() == []


def test_memory_detects_restored_timestamp_and_deletion(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("first", encoding="utf-8")
    memory = SimpleNamespace(
        _last_mtimes={}, _get_file_mtime=lambda p: AgentMemoryMiddleware._get_file_mtime(None, p)
    )
    AgentMemoryMiddleware._record_mtimes(memory, [path])
    stamp = path.stat().st_mtime_ns
    path.write_text("other", encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))
    assert AgentMemoryMiddleware._files_changed(memory, [path])
    AgentMemoryMiddleware._record_mtimes(memory, [path])
    path.unlink()
    assert AgentMemoryMiddleware._files_changed(memory, [])
