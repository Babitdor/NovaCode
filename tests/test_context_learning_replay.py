"""Paired task replays compare opt-in learning with no live model calls."""

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field

from novacode_cli.agents.context_compaction import ContextCompactionMiddleware
from novacode_cli.context.snapshot import ContextSnapshotMiddleware
from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.hermes.middleware import NovaLearningMiddleware
from tests.test_context_compaction import Worker, work, _well_formed, TASK


class ReplayModel(Worker, FakeListChatModel):
    responses: list[str] = Field(default_factory=lambda: ["unused"])
    review_requests: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if run_manager and run_manager.metadata.get("nova_oob"):
            self.review_requests.append(messages)
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage('<lesson topic="checks">- use the local checks</lesson>')
                    )
                ]
            )
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest.mark.asyncio
async def test_learning_on_off_replays_keep_constraints_pairing_and_no_unreviewed_promotion(
    tmp_path,
):
    outcomes = []
    for enabled in (False, True):
        model = ReplayModel(steps=15)
        directory = tmp_path / str(enabled)
        learning = NovaLearningMiddleware(
            InMemoryStore(), agent_dir=directory, project="a", enabled=enabled
        )
        agent = create_agent(
            model,
            tools=[work],
            middleware=[
                learning,
                ContextCompactionMiddleware(model, context_window=4000),
                ContextSnapshotMiddleware(),
            ],
            checkpointer=InMemorySaver(),
        )
        result = await agent.ainvoke(
            {"messages": [HumanMessage(TASK)]},
            {"configurable": {"thread_id": str(enabled)}, "recursion_limit": 200},
        )
        await learning.drain(timeout=10)
        outcomes.append(result["messages"][-1].content)
        assert model.summaries
        for messages in model.seen:
            _well_formed(messages)
            assert any(TASK in str(message.content) for message in messages)
        assert result["_context_snapshot"]["protected_tokens"] > 0
        if enabled:
            assert len(model.review_requests) == 1
            ledger = LearningLedger(directory / "learning.sqlite3")
            assert ledger.list(project="a", active=True) == []
            assert ledger.list(project="a")
        else:
            assert not model.review_requests
    assert outcomes == ["all done", "all done"]
