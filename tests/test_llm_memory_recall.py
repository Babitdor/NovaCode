"""The model interprets recall intent before deterministic retrieval can miss it."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from novacode_cli.memory.recall import MAX_CATALOG, parse_plan
from novacode_cli.memory.search import MemorySearchService

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemorySearchService:
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
    monkeypatch.setattr("novacode_cli.memory.semantic.similarities", lambda *_: [])
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "transport.md").write_text(
        "Use a JWT token during the WebSocket handshake.", encoding="utf-8"
    )
    foreign = memories / "projects" / "other"
    foreign.mkdir(parents=True)
    (foreign / "private.md").write_text("Foreign project detail", encoding="utf-8")
    return MemorySearchService(tmp_path, project_key="current")


@pytest.mark.parametrize(
    "plan",
    [
        {"queries": ["JWT WebSocket handshake"], "topics": []},
        {"queries": [], "topics": ["t0"]},
    ],
)
async def test_model_can_recall_without_original_keyword_matches(
    corpus: MemorySearchService, plan: dict
):
    question = "How did we solve the browser credential limitation?"
    assert "No memory matches" in await corpus.search(question)
    model = FakeListChatModel(responses=[json.dumps(plan)])
    result = await corpus.search(question, model=model)
    assert "JWT token during the WebSocket handshake" in result
    assert "/memories/memories/transport.md" in result
    assert "Foreign project" not in result


async def test_planner_only_sees_scoped_catalog_and_cannot_invent_topics(
    corpus: MemorySearchService,
):
    model = AsyncMock()
    model.ainvoke.return_value.content = '{"queries":[],"topics":["../other/private"]}'
    result = await corpus.search("WebSocket JWT", model=model)
    payload = json.loads(model.ainvoke.call_args.args[0][1].content)
    assert len(payload["catalog"]) == 1
    assert payload["catalog"][0]["topic"] == "transport"
    assert "Foreign project" not in str(payload)
    assert "JWT token" in result
    assert "LLM recall planning unavailable" in result


async def test_planning_timeout_falls_back_but_cancellation_propagates(
    corpus: MemorySearchService, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("novacode_cli.memory.recall.MODEL_TIMEOUT", 0.01)

    async def slow(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(10)

    model = AsyncMock()
    model.ainvoke.side_effect = slow
    result = await corpus.search("WebSocket JWT", model=model)
    assert "JWT token" in result
    assert "LLM recall planning unavailable" in result
    model.ainvoke.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await corpus.search("WebSocket JWT", model=model)


def test_catalog_is_bounded_and_plans_validate_all_fields():
    data = {f"topic-{i}": (frozenset(), "x" * 1000, frozenset()) for i in range(200)}
    catalog = MemorySearchService._recall_catalog(data)
    assert len(catalog) == MAX_CATALOG
    assert all(len(item["summary"]) <= 160 for item in catalog)
    for invalid in (
        '{"queries":["x"],"topics":["missing"]}',
        '{"queries":[""],"topics":[]}',
        '{"queries":["a","b","c","d"],"topics":[]}',
        '{"queries":[],"topics":[],"fact":"invented"}',
    ):
        with pytest.raises(ValueError, match="Recall plan"):
            parse_plan(invalid, {"t0": "transport"})
    plan = parse_plan(
        [
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": '{"queries":["JWT"],"topics":["t0"]}'},
        ],
        {"t0": "transport"},
    )
    assert plan.topics == ("transport",)
