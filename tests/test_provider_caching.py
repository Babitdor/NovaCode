"""Public-provider caching contracts, portable fallback and stable authorized inventories."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, tool

from novacode_cli.agents.provider_caching import native_tool_search, public_provider
from novacode_cli.agents.strict_tools import StrictToolMiddleware
from novacode_cli.agents.tool_search import ToolSearchMiddleware
from novacode_cli.memory.agent_memory import AgentMemoryMiddleware

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


@pytest.fixture(autouse=True)
def verified_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise candidate routes without enabling unverified production routes."""
    from novacode_cli.agents import provider_caching

    monkeypatch.setattr(
        provider_caching,
        "VERIFIED_NATIVE_ROUTES",
        frozenset(
            {
                ("anthropic", "claude-sonnet-4-6"),
                ("openai", "gpt-5.4"),
            }
        ),
    )


def test_unverified_routes_remain_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.agents import provider_caching

    monkeypatch.setattr(provider_caching, "VERIFIED_NATIVE_ROUTES", frozenset())
    for provider in ("anthropic", "openai"):
        prepared = ToolSearchMiddleware()._apply(request(adapter(provider)))
        assert [t.name for t in prepared.tools] == ["read_file"]
        assert "cache_control" not in prepared.model_settings


@tool
def read_file(path: str) -> str:
    """Read a file."""
    return path


@tool
def remote_lookup(query: str) -> str:
    """Look up remote records."""
    return query


def adapter(
    provider: str = "anthropic", name: str | None = None, endpoint: str | None = None
) -> BaseChatModel:
    name = name or ("claude-sonnet-4-6" if provider == "anthropic" else "gpt-5.4")
    metadata = {"nova_provider": provider, "nova_model": name}
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=name, api_key="test-only", metadata=metadata, base_url=endpoint)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=name,
        api_key="test-only",
        metadata=metadata,
        base_url=endpoint,
        use_responses_api=True,
    )


def request(
    model: BaseChatModel,
    messages: list[BaseMessage] | None = None,
    tools: list[BaseTool] | None = None,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=messages or [HumanMessage("hello")],
        system_message=SystemMessage("policy"),
        tools=tools if tools is not None else [remote_lookup, read_file],
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_native_inventory_is_stable_across_discovery_and_shrinks_on_removal(provider: str) -> None:
    model = adapter(provider)
    middleware = ToolSearchMiddleware()
    before = middleware._apply(request(model))
    after = middleware._apply(
        request(
            model,
            [
                AIMessage("", tool_calls=[{"name": "remote_lookup", "args": {}, "id": "r"}]),
            ],
        )
    )
    assert before.tools == after.tools
    deferred = next(t for t in before.tools if getattr(t, "name", "") == "remote_lookup")
    assert deferred.extras["defer_loading"] is True
    removed = middleware._apply(request(model, tools=[read_file]))
    assert not any(getattr(t, "name", "") == "remote_lookup" for t in removed.tools)
    assert remote_lookup.extras is None


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_final_native_payload_preserves_deferred_fields_through_strict_tools(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_STRICT_TOOLS", "auto")
    model = adapter(provider)
    prepared = StrictToolMiddleware()._prepare(ToolSearchMiddleware()._apply(request(model)))
    kwargs = model.bind_tools(prepared.tools, **prepared.model_settings).kwargs
    payload = model._get_request_payload([prepared.system_message, *prepared.messages], **kwargs)
    from novacode_cli.context.snapshot import ContextSnapshot

    snapshot = ContextSnapshot.capture(prepared)
    import json

    observed = json.loads(snapshot.provider_prepared[0])
    assert observed["tools"] == payload["tools"]
    assert observed.get("cache_control") == payload.get("cache_control")
    optional = next(t for t in payload["tools"] if t.get("name") == "remote_lookup")
    assert optional["defer_loading"] is True
    assert optional["strict"] is True
    if provider == "anthropic":
        assert payload["cache_control"] == {"type": "ephemeral"}
        assert any(t.get("type", "").startswith("tool_search") for t in payload["tools"])
    else:
        assert {"type": "tool_search"} in payload["tools"]
        assert "cache_control" not in payload
        assert "prompt_cache_retention" not in payload


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_gateways_and_unknown_models_remain_portable(provider: str) -> None:
    model = adapter(provider, endpoint="https://gateway.example/v1")
    assert public_provider(model) == ""
    assert native_tool_search(model) == ""
    prepared = ToolSearchMiddleware()._apply(request(model))
    assert [t.name for t in prepared.tools] == ["read_file"]
    assert "cache_control" not in prepared.model_settings
    assert native_tool_search(adapter(provider, name="unknown-model")) == ""
    model.metadata["nova_provider"] = "commandcode"
    assert native_tool_search(model) == ""
    assert isinstance(
        AgentMemoryMiddleware._make_system_message(
            SimpleNamespace(model=model, system_prompt="base"), "base", "volatile"
        ).content,
        str,
    )


def test_conversation_breakpoint_respects_limit() -> None:
    model = adapter()
    blocks = [
        {"type": "text", "text": str(i), "cache_control": {"type": "ephemeral"}} for i in range(4)
    ]
    prepared = ToolSearchMiddleware()._apply(
        request(model).override(
            system_message=SystemMessage(blocks),
        )
    )
    assert "cache_control" not in prepared.model_settings


def test_explicit_feature_rejection_retries_portable_once() -> None:
    class UnsupportedError(Exception):
        status_code = 400

    middleware = ToolSearchMiddleware()
    calls: list[ModelRequest] = []

    def handler(prepared: ModelRequest) -> str:
        calls.append(prepared)
        if len(calls) == 1:
            message = "defer_loading is not supported"
            raise UnsupportedError(message)
        return "ok"

    assert middleware.wrap_model_call(request(adapter()), handler) == "ok"
    assert len(calls) == 2
    assert [t.name for t in calls[1].tools] == ["read_file"]
    assert "cache_control" not in calls[1].model_settings


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (401, "unsupported tool_search"),
        (403, "unsupported tool_search"),
        (400, "invalid messages"),
        (429, "unsupported tool_search"),
        (400, "permission denied for unsupported tool_search"),
        (400, "unknown model: tool_search is unsupported"),
        (400, "tool_search request failed: unknown value in messages"),
    ],
)
def test_unrelated_rejection_is_not_retried(status: int, text: str) -> None:
    class RejectionError(Exception):
        status_code = status

    calls: list[ModelRequest] = []

    def handler(prepared: ModelRequest) -> str:
        calls.append(prepared)
        raise RejectionError(text)

    with pytest.raises(RejectionError):
        ToolSearchMiddleware().wrap_model_call(request(adapter()), handler)
    assert len(calls) == 1


async def test_async_feature_retry_and_cancellation() -> None:
    class UnsupportedError(Exception):
        status_code = 400

    middleware = ToolSearchMiddleware()
    middleware._frequent = set()
    calls: list[ModelRequest] = []

    async def handler(prepared: ModelRequest) -> str:
        calls.append(prepared)
        if len(calls) == 1:
            message = "tool_search is unsupported"
            raise UnsupportedError(message)
        return "ok"

    assert await middleware.awrap_model_call(request(adapter()), handler) == "ok"
    assert len(calls) == 2
    assert [t.name for t in calls[1].tools] == ["read_file"]
    cancelled = ToolSearchMiddleware()
    cancelled._frequent = set()

    async def cancel(prepared: ModelRequest) -> str:
        del prepared
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancelled.awrap_model_call(request(adapter()), cancel)
    assert not cancelled._portable_routes


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_native_search_reference_blocks_survive_repair_and_payload(provider: str) -> None:
    from novacode_cli.agents.tool_call_repair import sanitize_history

    if provider == "anthropic":
        blocks = [
            {
                "type": "server_tool_use",
                "id": "srv_1",
                "name": "tool_search_tool_regex",
                "input": {"query": "lookup"},
            },
            {
                "type": "tool_search_tool_result",
                "tool_use_id": "srv_1",
                "content": [
                    {"type": "tool_reference", "tool_name": "remote_lookup"},
                ],
            },
        ]
    else:
        blocks = [
            {
                "type": "tool_search_call",
                "id": "ts_1",
                "call_id": "ts_1",
                "arguments": "lookup",
                "status": "completed",
            },
            {
                "type": "tool_search_output",
                "id": "ts_2",
                "call_id": "ts_1",
                "tools": [{"type": "function", "name": "remote_lookup"}],
            },
        ]
    model = adapter(provider)
    history = sanitize_history([HumanMessage("lookup"), AIMessage(content=blocks)])
    assert history[-1].content == blocks
    prepared = ToolSearchMiddleware()._apply(request(model, history))
    kwargs = model.bind_tools(prepared.tools, **prepared.model_settings).kwargs
    payload = model._get_request_payload([prepared.system_message, *prepared.messages], **kwargs)
    if provider == "anthropic":
        assert payload["messages"][-1]["content"] == blocks
    else:
        assert payload["input"][-2:] == blocks
