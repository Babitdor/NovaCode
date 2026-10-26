"""Inspect real SDK request payloads without making model calls."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from novacode_cli.config import model_create


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.config import openai_chatgpt_auth
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr(
        NovaConfig,
        "get",
        lambda _self, key, default=None: {
            "reasoning_effort": "high",
            "thinking_budget": 0,
        }.get(key, default),
    )
    monkeypatch.setattr(openai_chatgpt_auth, "selected_auth_mode", lambda: "api_key")
    monkeypatch.setattr(NovaConfig, "get_model_base_url", lambda _self: None)
    for provider, env in model_create.PROVIDER_KEY_ENV.items():
        monkeypatch.setenv(env, "test-key")
        # Newer providers resolve keys from the keychain/env and have no field.
        if hasattr(model_create.settings, f"{provider}_api_key"):
            monkeypatch.setattr(model_create.settings, f"{provider}_api_key", "test-key")


@pytest.mark.parametrize(
    ("provider", "name", "key", "expected"),
    [
        ("openai", "gpt-5.5", "reasoning", {"effort": "high", "summary": "auto"}),
        ("ollama", "qwen3:8b", "reasoning_effort", "high"),
        ("commandcode", "gpt-5.5", "reasoning_effort", "high"),
        ("commandcode", "deepseek/deepseek-v4-flash", "reasoning_effort", "high"),
        ("opencode", "deepseek-v4.1-flash", "reasoning_effort", "high"),
        ("opencode_zen", "deepseek-v4-flash", "reasoning_effort", "high"),
        ("openrouter", "deepseek/deepseek-r1", "reasoning", {"effort": "high"}),
        ("anthropic", "claude-sonnet-4-6", "output_config", {"effort": "high"}),
        ("commandcode", "claude-sonnet-4-6", "output_config", {"effort": "high"}),
        ("opencode", "claude-sonnet-4-6", "output_config", {"effort": "high"}),
        ("opencode_zen", "claude-sonnet-4-6", "output_config", {"effort": "high"}),
    ],
)
@pytest.mark.usefixtures("configured")
def test_constructed_sdk_payload_contains_the_configured_effort(
    provider: str, name: str, key: str, expected: object
):
    model = model_create.build_chat_model(provider, name)
    payload = model._get_request_payload([HumanMessage("hello")])
    assert payload.get(key, payload.get("extra_body", {}).get(key)) == expected


@pytest.mark.parametrize("provider", ["commandcode", "opencode", "opencode_zen"])
def test_gateway_deepseek_medium_maps_to_high_and_off_requests_minimum(provider: str):
    helper = model_create.gateway_reasoning_kwargs
    assert helper(provider, "deepseek/deepseek-v4.1-flash", "medium") == {
        "reasoning_effort": "high"
    }
    assert helper(provider, "deepseek/deepseek-v4.1-flash", "off") == {"reasoning_effort": "low"}
    assert helper(provider, "unknown-model", "high") == {}
    assert "No verified" in model_create.reasoning_setting_note(provider, "unknown-model", "high")


def test_default_leaves_gateway_settings_to_provider():
    assert model_create.gateway_reasoning_kwargs("openrouter", "deepseek/r1", None) == {}
    assert model_create.gateway_reasoning_kwargs("openrouter", "deepseek/r1", "default") == {}


@pytest.mark.usefixtures("configured")
def test_custom_openai_endpoints_keep_chat_completions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.invalid/v1")
    model = model_create.build_chat_model("openai", "gpt-5.5")
    payload = model._get_request_payload([HumanMessage("hi")])
    assert payload["reasoning_effort"] == "high"
    assert "reasoning" not in payload
    assert "messages" in payload


@pytest.mark.usefixtures("configured")
def test_azure_v1_reasoning_models_request_summaries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "offline")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com")
    model = model_create.build_chat_model("azure", "gpt-5.5")
    payload = model._get_request_payload([HumanMessage("hi")])
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert payload["store"] is False


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("provider", ["commandcode", "openrouter"])
def test_gateway_effort_reaches_the_serialized_http_body(provider: str):
    import json

    import httpx
    from openai import OpenAI

    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    model = model_create.build_chat_model(provider, "deepseek/deepseek-v4-flash")
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client = OpenAI(
            api_key="offline", base_url="https://offline.invalid/v1", http_client=http_client
        )
        model.client = client.chat.completions
        assert model.invoke("hello").content == "done"
    assert len(requests) == 1
    if provider == "commandcode":
        assert requests[0]["reasoning_effort"] == "high"
    else:
        assert requests[0]["reasoning"] == {"effort": "high"}
    assert "extra_body" not in requests[0]
