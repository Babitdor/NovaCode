"""Wire contracts, all registered providers, and validation before execution."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from novacode_cli.agents.strict_tools import (
    PROVIDER_STRICT_MODES,
    IncompatibleStrictSchema,
    StrictToolMiddleware,
    provider_route,
    strict_schema,
)


@tool
def count_items(count: int, label: str = "default") -> str:
    """Count labeled items."""
    return f"{count}:{label}"


@tool
def freeform(payload: dict) -> str:
    """Process an open-ended map without losing its keys."""
    return str(payload)


@pytest.fixture(autouse=True)
def isolate_switch(monkeypatch):
    monkeypatch.setenv("NOVA_STRICT_TOOLS", "auto")


def model(provider, name="gpt-4o", endpoint=None):
    return SimpleNamespace(
        metadata={"nova_provider": provider, "nova_model": name}, openai_api_base=endpoint
    )


def request(provider="openai", name="gpt-4o", tools=None, endpoint=None):
    return ModelRequest(
        model=model(provider, name, endpoint), messages=[], tools=tools or [count_items]
    )


def test_every_provider_is_explicitly_audited():
    from novacode_cli.config.model_manager import MODEL_PRESETS

    assert set(PROVIDER_STRICT_MODES) == set(MODEL_PRESETS)


@pytest.mark.parametrize("provider,route", list(PROVIDER_STRICT_MODES.items()))
def test_provider_route_defaults(provider, route):
    name = {"anthropic": "claude-sonnet-4-5", "cerebras": "gpt-oss-120b"}.get(provider, "gpt-4o")
    endpoint = "https://example.openai.azure.com/openai/v1" if provider == "azure" else None
    expected = "local" if route in {"conditional", "bedrock"} else route
    assert provider_route(model(provider, name, endpoint), "auto") == expected


def test_openai_wire_schema_is_strict_without_mutating_tools_or_inventing_null():
    middleware = StrictToolMiddleware()
    original = copy.deepcopy(count_items.tool_call_schema.model_json_schema())
    result = middleware._prepare(request())
    function = result.tools[0]["function"]
    assert function["strict"] is True
    assert function["parameters"]["required"] == ["count", "label"]
    assert function["parameters"]["additionalProperties"] is False
    assert function["parameters"]["properties"]["label"]["type"] == "string"
    assert "default" in function["parameters"]["properties"]["label"]["description"]
    assert count_items.tool_call_schema.model_json_schema() == original

    from langchain_openai import ChatOpenAI

    bound = ChatOpenAI(model="gpt-4o", api_key="fixture-not-a-key").bind_tools(result.tools)
    assert bound.kwargs["tools"][0]["function"]["strict"] is True


def test_mcp_accounting_metadata_survives_but_is_not_sent_to_provider():
    import json

    tagged = count_items.model_copy(update={"metadata": {"nova_mcp_server": "database"}})
    result = StrictToolMiddleware()._prepare(request(tools=[tagged]))
    assert result.tools[0].metadata == {"nova_mcp_server": "database"}
    assert "nova_mcp_server" not in json.dumps(result.tools[0])


def test_anthropic_wire_flag_survives_the_actual_sdk_adapter():
    from langchain_anthropic import ChatAnthropic

    prepared = StrictToolMiddleware()._prepare(request("anthropic", "claude-sonnet-4-5"))
    definition = prepared.tools[0]
    assert definition["strict"] is True and "input_schema" in definition
    bound = ChatAnthropic(model_name="claude-sonnet-4-5", api_key="fixture-not-a-key").bind_tools(
        prepared.tools
    )
    assert bound.kwargs["tools"][0]["strict"] is True


def test_google_uses_validated_not_forced_tool_calling():
    result = StrictToolMiddleware()._prepare(request("google", "gemini-2.5-flash"))
    assert result.model_settings["tool_config"]["function_calling_config"]["mode"] == "VALIDATED"
    assert result.tools[0] is count_items
    request_with_map = request("google", "gemini-2.5-flash", tools=[freeform])
    assert StrictToolMiddleware()._prepare(request_with_map) is request_with_map


def test_azure_disables_parallel_and_custom_openai_endpoint_is_not_trusted():
    mw = StrictToolMiddleware()
    result = mw._prepare(request("azure", endpoint="https://demo.openai.azure.com/openai/v1"))
    assert result.model_settings["parallel_tool_calls"] is False
    custom = request(endpoint="https://custom.example/v1")
    assert mw._prepare(custom) is custom


def test_bedrock_checks_model_and_sdk_shape_before_setting_tool_spec():
    req = request("bedrock", "global.anthropic.claude-sonnet-4-5-v1:0")
    req.model.client = SimpleNamespace(
        meta=SimpleNamespace(
            service_model=SimpleNamespace(
                shape_for=lambda name: SimpleNamespace(members={"strict": object()})
            )
        )
    )
    result = StrictToolMiddleware()._prepare(req)
    assert result.tools[0]["toolSpec"]["strict"] is True


def test_explicit_mode_for_gateway_requires_parameter_support(monkeypatch):
    monkeypatch.setenv("NOVA_STRICT_TOOLS", "on")
    req = request("openrouter", "openai/gpt-4o")
    result = StrictToolMiddleware()._prepare(req)
    assert result.model_settings["extra_body"]["provider"]["require_parameters"] is True
    assert result.tools[0]["function"]["strict"] is True
    with pytest.raises(ValueError, match="not verified"):
        StrictToolMiddleware()._prepare(request("ollama", "local-model"))


def test_open_maps_and_unsupported_constraints_are_not_silently_destroyed(monkeypatch):
    req = request(tools=[freeform])
    assert StrictToolMiddleware()._prepare(req).tools[0] is freeform
    with pytest.raises(IncompatibleStrictSchema):
        strict_schema({"type": "string", "pattern": "^[A-Z]+$"})
    monkeypatch.setenv("NOVA_STRICT_TOOLS", "on")
    with pytest.raises(ValueError, match="freeform"):
        StrictToolMiddleware()._prepare(req)


@pytest.mark.parametrize(
    "args", [{"count": "2"}, {"count": True}, {"label": "missing"}, {"count": 2, "unexpected": 3}]
)
def test_raw_arguments_are_rejected_before_pydantic_coercion_or_execution(args):
    mw = StrictToolMiddleware()
    call = {"name": "count_items", "args": args, "id": "c", "type": "tool_call"}
    req = ToolCallRequest(call, count_items, {}, None)
    result = mw.wrap_tool_call(req, lambda _: pytest.fail("invalid call executed"))
    assert isinstance(result, ToolMessage) and result.status == "error"


def test_off_preserves_legacy_coercion_and_valid_call_keeps_defaults(monkeypatch):
    mw = StrictToolMiddleware()
    call = {"name": "count_items", "args": {"count": 2}, "id": "c", "type": "tool_call"}
    req = ToolCallRequest(call, count_items, {}, None)
    assert mw.wrap_tool_call(req, lambda r: r.tool.invoke(r.tool_call)).content == "2:default"
    monkeypatch.setenv("NOVA_STRICT_TOOLS", "off")
    call["args"]["count"] = "2"
    assert mw.wrap_tool_call(req, lambda r: r.tool.invoke(r.tool_call)).content == "2:default"


def test_external_schema_references_are_blocked_without_network_access():
    from langchain_core.tools import StructuredTool

    external = StructuredTool.from_function(
        lambda value: value,
        name="external",
        description="test",
        args_schema={
            "type": "object",
            "properties": {"value": {"$ref": "https://invalid.example/schema"}},
        },
    )
    call = {"name": "external", "args": {"value": 1}, "id": "c", "type": "tool_call"}
    result = StrictToolMiddleware().wrap_tool_call(
        ToolCallRequest(call, external, {}, None),
        lambda _: pytest.fail("external reference executed"),
    )
    assert result.status == "error" and "External" in result.content


@pytest.mark.asyncio
async def test_agent_graph_binds_strict_schema_but_rejects_bad_arguments_before_execution():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    schemas = []
    executions = []

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            schemas.extend(tools)
            return self

    @tool
    def execute(count: int) -> str:
        """Run a typed operation."""
        executions.append(count)
        return "success"

    scripted = Model(
        metadata={"nova_provider": "openai", "nova_model": "gpt-4o"},
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "execute", "id": "bad", "args": {"count": "2"}}],
                ),
                AIMessage(
                    content="", tool_calls=[{"name": "execute", "id": "good", "args": {"count": 2}}]
                ),
                AIMessage(content="done"),
            ]
        ),
    )
    graph = create_agent(scripted, tools=[execute], middleware=[StrictToolMiddleware()])
    result = await graph.ainvoke({"messages": [HumanMessage(content="run operation")]})
    outputs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.status for m in outputs] == ["error", "success"]
    assert executions == [2]
    assert schemas[0]["function"]["strict"] is True


def test_background_agents_receive_strict_middleware(tmp_path, monkeypatch):
    from novacode_cli.context.snapshot import background_context_middleware

    monkeypatch.setattr("novacode_cli.agents.async_workspace.workspace_root", lambda: tmp_path)
    assert sum(isinstance(m, StrictToolMiddleware) for m in background_context_middleware()) == 1
