"""Provider-boundary accounting without credentials or network calls."""

from dataclasses import FrozenInstanceError

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from novacode_cli.context.snapshot import (
    ContextSnapshot,
    ContextSnapshotMiddleware,
    ProtectedContextOverflowError,
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
