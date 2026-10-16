"""Provider-boundary accounting without credentials or network calls."""

from dataclasses import FrozenInstanceError

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from novacode_cli.context.snapshot import (
    ContextSnapshot,
    ContextSnapshotMiddleware,
    ProtectedContextOverflowError,
    SnapshotState,
)


def test_snapshot_is_immutable_and_reports_schema_changes_and_reservation():
    model = FakeListChatModel(responses=["ok"])
    tool = {
        "type": "function",
        "function": {
            "name": "read",
            "description": "read",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    request = ModelRequest(
        model=model,
        messages=[HumanMessage("Constraint in the middle")],
        tools=[tool],
        system_message=SystemMessage("standing policy"),
        state={},
        model_settings={"max_tokens": 100},
    )
    snapshot = ContextSnapshot.capture(request)
    with pytest.raises(FrozenInstanceError):
        snapshot.model = "changed"
    request.messages[0].content = "changed"
    assert "Constraint" in snapshot.messages[-1]
    assert snapshot.output_reservation == 100
    assert snapshot.schemas
    assert "Constraint" not in str(snapshot.diagnostics())
    with pytest.raises(ProtectedContextOverflowError):
        snapshot.validate(50)


def test_real_graph_persists_only_content_free_final_request_accounting():
    model = FakeListChatModel(responses=["done"])
    agent = create_agent(
        model=model,
        system_prompt="secret standing policy",
        middleware=[ContextSnapshotMiddleware()],
    )
    result = agent.invoke({"messages": [HumanMessage("private task")]})
    diagnostics = result["_context_snapshot"]
    assert diagnostics["message_count"] == 2
    assert diagnostics["protected_tokens"] > 0
    assert "private task" not in str(diagnostics)
    assert "secret standing policy" not in str(diagnostics)


def test_parallel_snapshot_updates_keep_one_complete_diagnostic():
    """Parallel graph nodes may return accounting in the same graph step."""
    first = {"generation": "first", "estimated_input_tokens": 100, "old_only": True}
    second = {"generation": "second", "estimated_input_tokens": 200}
    graph = StateGraph(SnapshotState)
    graph.add_node("first", lambda _state: {"_context_snapshot": first})
    graph.add_node("second", lambda _state: {"_context_snapshot": second})
    for branch in ("first", "second"):
        graph.add_edge(START, branch)
        graph.add_edge(branch, END)
    result = graph.compile().invoke({"messages": [HumanMessage("task")]})
    assert result["_context_snapshot"] in (first, second)


def test_later_snapshot_replaces_diagnostics_instead_of_merging_fields():
    graph = StateGraph(SnapshotState)
    graph.add_node(
        "first", lambda _state: {"_context_snapshot": {"generation": "first", "old_only": True}}
    )
    graph.add_node("second", lambda _state: {"_context_snapshot": {"generation": "second"}})
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    result = graph.compile().invoke({"messages": [HumanMessage("task")]})
    assert result["_context_snapshot"] == {"generation": "second"}
