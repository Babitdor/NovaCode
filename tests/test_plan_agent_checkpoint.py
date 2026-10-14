"""Plan mode writes to the session's fenced checkpointer, so it must hold a lease."""

from typing import TypedDict

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from novacode_cli.session.checkpoint_coordinator import CoordinatedGraph, coordinate_saver


class _State(TypedDict):
    n: int


@pytest.mark.asyncio
async def test_plan_mode_checkpoints_on_the_shared_coordinated_saver(tmp_path, monkeypatch):
    import deepagents.graph as dg
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.plan_agent import create_plan_agent_with_config

    def _graph(**kw):
        # Stands in for the deep agent: one step, checkpointed on what it is given.
        g = StateGraph(_State)
        g.add_node("step", lambda s: {"n": s["n"] + 1})
        g.add_edge(START, "step")
        g.add_edge("step", END)
        return g.compile(checkpointer=kw["checkpointer"])

    monkeypatch.setattr(dg, "create_deep_agent", _graph)
    async with aiosqlite.connect(tmp_path / "cp.db") as conn:
        saver = AsyncSqliteSaver(conn)
        main = coordinate_saver(saver)  # what the core agent build does first
        plan, _ = create_plan_agent_with_config(
            model=FakeListChatModel(responses=["x"]), assistant_id="nova", tools=[],
            checkpointer=saver,
        )
        assert isinstance(plan, CoordinatedGraph)
        assert plan.coordinator is main  # one lock per thread across both agents
        config = {"configurable": {"thread_id": "t1"}}
        # Raised "Checkpoint mutation requires coordinated ownership" unwrapped.
        assert await plan.ainvoke({"n": 0}, config) == {"n": 1}
        assert (await plan.aget_state(config)).values == {"n": 1}
