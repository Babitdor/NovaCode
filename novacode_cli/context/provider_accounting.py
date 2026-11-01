"""Supported provider preparation and content-free HTTP attempt observation."""

# JSON and LangGraph interfaces carry heterogeneous values.
# ruff: noqa: ANN401
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class AttemptCounter:
    """One dispatch's real HTTP attempts; no URLs, headers, or body retained."""

    count: int = 0
    observed: bool = False


http_attempts: ContextVar[AttemptCounter | None] = ContextVar("nova_http_attempts", default=None)


def _observe(request: Any) -> None:
    del request  # Never inspect headers, body, URL, or credentials.
    counter = http_attempts.get()
    if counter:
        counter.count += 1


async def _aobserve(request: Any) -> None:
    _observe(request)


def instrument_http(model: Any) -> bool:
    """Attach HTTPX hooks to supported SDK clients without changing transports."""
    supported = False
    for name, callback in (
        ("root_client", _observe),
        ("root_async_client", _aobserve),
        ("_client", _observe),
        ("_async_client", _aobserve),
    ):
        sdk = getattr(model, name, None)
        client = getattr(sdk, "_client", None)
        hooks = getattr(client, "event_hooks", None)
        if isinstance(hooks, dict) and "request" in hooks:
            if callback not in hooks["request"]:
                hooks["request"].append(callback)
            supported = True
    return supported


def prepared_request(request: Any, messages: Any, schemas: Any) -> Any:
    """Prepare OpenAI/Anthropic payloads using their installed adapter code."""
    model = request.model
    # Inspect adapter ancestry before importing optional providers. Nova's
    # gateway wrappers live in model_create but inherit the installed adapter.
    modules = tuple(base.__module__ for base in type(model).__mro__)
    anthropic = openai = False
    if any(module.startswith("langchain_anthropic.") for module in modules):
        from langchain_anthropic import ChatAnthropic

        anthropic = isinstance(model, ChatAnthropic)
    elif any(module.startswith("langchain_openai.") for module in modules):
        from langchain_openai import ChatOpenAI

        openai = isinstance(model, ChatOpenAI)
    if not (openai or anthropic):
        return (), None
    prepare = getattr(model, "_get_request_payload", None)
    if prepare is None:
        return (), None
    settings = dict(getattr(request, "model_settings", None) or {})
    if schemas:
        # The adapters' binding paths preserve built-ins and provider extras;
        # generic schema conversion loses server search and deferred definitions.
        settings["tools"] = model.bind_tools(request.tools).kwargs["tools"]
    response_format = getattr(request, "response_format", None)
    if response_format is not None:
        from langchain.agents.structured_output import ProviderStrategy, ToolStrategy

        if isinstance(response_format, ProviderStrategy) and openai:
            settings.update(response_format.to_model_kwargs())
        elif isinstance(response_format, ToolStrategy) and openai:
            tools = list(settings.get("tools", []))
            tools.extend(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description or "",
                        "parameters": spec.json_schema,
                    },
                }
                for spec in response_format.schema_specs
            )
            settings["tools"] = tools
        else:
            return (), None
    payload = prepare(messages, **settings)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    reservation = next(
        (
            payload.get(key)
            for key in ("max_output_tokens", "max_completion_tokens", "max_tokens")
            if payload.get(key) is not None
        ),
        0,
    )
    return (encoded,), int(reservation)
