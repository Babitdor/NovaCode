"""Provider preparation and real HTTP attempts, using local MockTransports."""

import json

import httpx
import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from novacode_cli.context.provider_accounting import AttemptCounter, http_attempts, instrument_http
from novacode_cli.context.snapshot import ContextSnapshot


def test_prepared_request_keeps_multimodal_transform_and_effective_output_limit():
    model = ChatOpenAI(
        api_key="fixture-only",
        model="gpt-4.1",
        max_tokens=123,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    request = ModelRequest(
        model=model,
        tools=[],
        state={},
        messages=[
            HumanMessage(
                [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AQID"}},
                ]
            )
        ],
    )
    snapshot = ContextSnapshot.capture(request)
    prepared = json.loads(snapshot.provider_prepared[0])
    assert prepared["response_format"] == {"type": "json_object"}
    assert prepared["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,AQID"
    assert snapshot.output_reservation == 123
    assert snapshot.confidence == "provider_prepared_estimate"
    diagnostic = snapshot.diagnostics()
    assert "describe" not in json.dumps(diagnostic)
    assert "AQID" not in json.dumps(diagnostic)
    assert diagnostic["provider_prepared_bytes"] > 0


def test_provider_strategy_schema_is_in_prepared_accounting():
    from langchain.agents.structured_output import ProviderStrategy
    from pydantic import BaseModel

    class Result(BaseModel):
        answer: str

    model = ChatOpenAI(api_key="fixture-only", model="gpt-4.1")
    request = ModelRequest(
        model=model,
        tools=[],
        state={},
        messages=[HumanMessage("schema")],
        response_format=ProviderStrategy(Result),
    )
    snapshot = ContextSnapshot.capture(request)
    prepared = json.loads(snapshot.provider_prepared[0])
    assert (
        prepared["response_format"]["json_schema"]["schema"]["properties"]["answer"]["type"]
        == "string"
    )
    plain = ContextSnapshot.capture(request.override(response_format=None))
    assert snapshot.estimated_input_tokens > plain.estimated_input_tokens
    assert snapshot.protected_tokens > plain.protected_tokens
    assert snapshot.generation != plain.generation


@pytest.mark.asyncio
async def test_http_hook_counts_actual_retry_attempts_without_retaining_content():
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": {"message": "fixture retry"}})
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4.1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(
            api_key="fixture-only", model="gpt-4.1", max_retries=1, http_async_client=client
        )
        assert instrument_http(model)
        assert instrument_http(model)  # Installation is idempotent.
        counter = AttemptCounter(observed=True)
        token = http_attempts.set(counter)
        try:
            response = await model.ainvoke([HumanMessage("sensitive fixture prompt")])
            assert response.content == "done"
            assert counter.count == 2
            assert vars(counter) == {"count": 2, "observed": True}
        finally:
            http_attempts.reset(token)


def test_anthropic_preparation_transforms_tool_schema_and_unknown_formats_are_unavailable():
    from langchain_anthropic import ChatAnthropic
    from langchain.agents.structured_output import ProviderStrategy
    from pydantic import BaseModel
    from novacode_cli.context.provider_accounting import prepared_request

    class Result(BaseModel):
        answer: str
    model = ChatAnthropic(api_key="fixture-only", model="claude-sonnet-4-5", max_tokens=234)
    schema = {"type": "function", "function": {"name": "verify", "parameters": {
        "type": "object", "properties": {"target": {"type": "string"}}}}}
    request = ModelRequest(model=model, tools=[], state={}, messages=[HumanMessage("verify")])
    payloads, reservation = prepared_request(request, request.messages, [json.dumps(schema)])
    payload = json.loads(payloads[0])
    assert payload["tools"][0]["name"] == "verify"
    assert payload["tools"][0]["input_schema"] == schema["function"]["parameters"]
    assert reservation == 234
    assert prepared_request(request.override(response_format=ProviderStrategy(Result)),
                            request.messages, []) == ((), None)
    assert instrument_http(object()) is False
