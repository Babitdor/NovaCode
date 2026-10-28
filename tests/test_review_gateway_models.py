"""Real SDK subclasses receive bounded learning reviews without losing their transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langgraph.store.memory import InMemoryStore

from novacode_cli.config.model_create import _opencode_chat_class
from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.hermes.review import ReviewRunner
from novacode_cli.hermes.tracker import ToolUsageTracker

if TYPE_CHECKING:
    from pathlib import Path


class Request(SimpleNamespace):
    def override(self, **updates: Any) -> Request:
        return Request(**{**vars(self), **updates})


class GatewayClaude(ChatAnthropic):
    pass


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("reject", [False, True])
async def test_sdk_gateway_reviews_prepare_candidates(
    protocol: str,
    *,
    reject: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import anthropic

    observed = []

    async def serve(_transport: object, request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if reject:
            return httpx.Response(
                503,
                json={"error": {"message": "offline rejection", "type": "server_error"}},
                request=request,
            )
        payload = json.loads(request.content)
        text = '<lesson topic="checks">Run repository checks.</lesson>'
        if protocol == "anthropic":
            assert request.headers["x-custom-auth"] == "preserved"
            assert payload["max_tokens"] == 4096
            reply = {
                "id": "msg-test",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        elif protocol == "responses":
            assert request.url.path.endswith("/responses")
            assert payload["max_output_tokens"] == 4096
            reply = {
                "id": "resp-test",
                "object": "response",
                "created_at": 1,
                "model": "test",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg-test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        else:
            assert request.url.path.endswith("/chat/completions")
            assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 4096
            reply = {
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
        return httpx.Response(200, json=reply, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", serve)
    if protocol == "anthropic":
        model = GatewayClaude(model="test", api_key="offline", max_retries=5)
        # This represents a gateway-managed bearer transport. Reconstructing
        # the cached SDK from the generic model fields would lose its auth.
        model.__dict__["_async_client"] = anthropic.AsyncAnthropic(
            api_key="offline",
            max_retries=5,
            http_client=httpx.AsyncClient(headers={"x-custom-auth": "preserved"}),
        )
        original = model._async_client
    else:
        model = _opencode_chat_class()(
            model="test",
            api_key="offline",
            max_retries=5,
            use_responses_api=protocol == "responses",
            http_async_client=httpx.AsyncClient(),
        )
        original = model.root_async_client
    store = InMemoryStore()
    tracker = ToolUsageTracker(store, owner="gateway-test")
    await tracker.record_tool_usage(
        "edit_file", success=True, outcome="success", evidence_source="structured"
    )
    runner = ReviewRunner(store, tracker, None, agent_dir=tmp_path, project="test")
    await runner.run_review(
        Request(model=model, messages=[HumanMessage("Finish a task")], state={})
    )
    if reject:
        assert len(observed) == 2  # One initial request and one bounded retry.
        assert not (tmp_path / "learning.sqlite3").exists()
        skips = await store.asearch(runner._namespace("review_skips"))
        assert skips[0].value["reason"] == "review call failed"
    else:
        assert len(observed) == 1
        records = LearningLedger(tmp_path / "learning.sqlite3").list()
        assert any(r.kind == "lesson" and r.status == "candidate" for r in records)
    assert original.max_retries == model.max_retries == 5
    if protocol == "anthropic":
        assert model._async_client is original
    else:
        assert model.root_async_client is original
    await original.close()
