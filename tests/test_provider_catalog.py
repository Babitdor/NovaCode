"""Every requested provider is routed and exercised without external requests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.tools import tool

from novacode_cli.config.provider_registry import CATALOG_PROVIDERS


@tool
def catalog_echo(text: str) -> str:
    """Echo a string for offline provider tool-binding checks."""
    return text


@pytest.fixture
def catalog_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.config import credentials

    monkeypatch.setattr(credentials, "get_stored_key", lambda _var: None)
    for preset in CATALOG_PROVIDERS.values():
        for var in (preset.get("api_key_var"), preset.get("base_url_env"), *preset["required_env"]):
            if var:
                monkeypatch.delenv(var, raising=False)
    for var, value in {
        "CLOUDFLARE_ACCOUNT_ID": "test-account",
        "CLOUDFLARE_GATEWAY_ID": "test-gateway",
        "SNOWFLAKE_ACCOUNT": "test-account",
        "DATABRICKS_HOST": "workspace.example.test",
        "NEON_AI_GATEWAY_BASE_URL": "https://neon.example.test",
        "INFOMANIAK_PRODUCT_ID": "1234",
        "QVAC_BASE_URL": "http://127.0.0.1:11434/v1",
        "NHR_FAU_BASE_URL": "https://nhr.example.test/v1",
        "WATSONX_URL": "https://us-south.ml.cloud.ibm.com",
        "WATSONX_AI_PROJECT_ID": "test-project",
        "SAP_AI_DEPLOYMENT_ID": "test-deployment",
    }.items():
        monkeypatch.setenv(var, value)


def test_every_requested_name_is_accounted_for():
    from novacode_cli.config.model_manager import MODEL_PRESETS
    from novacode_cli.config.provider_auth import SERVICE_DISPLAY_NAMES

    names = {preset["name"].casefold() for preset in MODEL_PRESETS.values()}
    names.update(name.casefold() for name in SERVICE_DISPLAY_NAMES.values())
    # Preserve established Nova ids/display names while recognizing regional
    # catalog labels and service names that have explanatory UI suffixes.
    aliases = {
        "Amazon Bedrock": "bedrock",
        "CommandCode": "commandcode",
        "Azure": "azure",
        "Nvidia": "nvidia",
        "MiniMax (minimax.io)": "minimax",
        "Kimi For Coding (kimi.com)": "kimi_coding",
        "Tavily": "tavily",
    }
    for name, provider in aliases.items():
        assert provider in MODEL_PRESETS or provider == "tavily"
        names.add(name.casefold())
    requested = (
        Path(__file__)
        .with_name("fixtures")
        .joinpath("requested_providers.txt")
        .read_text()
        .splitlines()
    )
    assert not {name for name in requested if name.casefold() not in names}


def test_catalog_registration_auth_and_shell_boundaries():
    from novacode_cli.config.model_create import PROVIDER_KEY_ENV, VALID_ASYNC_PROVIDERS
    from novacode_cli.config.model_manager import MODEL_PRESETS
    from novacode_cli.config.provider_auth import credential_names
    from novacode_cli.shell.utils import _sanitize_env

    credentials = credential_names()
    for provider, preset in CATALOG_PROVIDERS.items():
        assert MODEL_PRESETS[provider] is preset
        if key := preset["api_key_var"]:
            assert credentials[provider] == key
            assert PROVIDER_KEY_ENV[provider] == key
            assert provider in VALID_ASYNC_PROVIDERS
            assert key not in _sanitize_env({key: "never-export-this"})


WIRED = sorted(
    p
    for p, record in CATALOG_PROVIDERS.items()
    if record["protocol"] != "vertex" and not record.get("unavailable_reason")
)


@pytest.mark.parametrize("provider", WIRED)
@pytest.mark.usefixtures("catalog_environment")
def test_every_provider_sends_a_tool_request_to_its_own_api(  # noqa: PLR0915 — one wire contract per provider
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """Use the real installed SDKs and HTTP transports, including token exchanges."""
    from novacode_cli.config.model_create import build_chat_model

    preset = CATALOG_PROVIDERS[provider]
    key = "offline-key-" + provider
    if provider == "sap-ai-core":
        key = json.dumps(
            {
                "url": "https://sap-auth.example.test",
                "clientid": "test-client",
                "clientsecret": "test-secret",
                "serviceurls": {"AI_API_URL": "https://sap-api.example.test"},
            }
        )
    monkeypatch.setenv(preset["api_key_var"], key)
    observed = []

    def serve(_transport: object, request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path.endswith(("/identity/token", "/oauth/token")):
            return httpx.Response(
                200, json={"access_token": "exchanged-token", "expires_in": 3600}, request=request
            )
        if request.url.path.endswith("/direct_access"):
            return httpx.Response(
                200,
                json={"token": "exchanged-token", "headers": {"x-gitlab-realm": "saas"}},
                request=request,
            )
        payload = json.loads(request.content)
        assert payload.get("tools"), (provider, payload)
        assert "never-export-this" not in request.content.decode()
        if provider in {"sap-ai-core", "watsonx", "gitlab"}:
            assert request.headers["authorization"] == "Bearer exchanged-token"
            assert "x-api-key" not in request.headers
        if request.url.path.endswith("/messages"):
            assert payload["tools"][0]["name"] == "catalog_echo"
            reply = {
                "id": "msg-offline",
                "type": "message",
                "role": "assistant",
                "model": payload["model"],
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        elif request.url.path.endswith("/responses"):
            assert payload["tools"][0]["name"] == "catalog_echo"
            reply = {
                "id": "resp-offline",
                "object": "response",
                "created_at": 1,
                "model": payload["model"],
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg-offline",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "OK", "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        else:
            assert request.url.path.endswith(("/chat/completions", "/text/chat"))
            assert payload["tools"][0]["function"]["name"] == "catalog_echo"
            reply = {
                "id": "chat-offline",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
            }
        return httpx.Response(200, json=reply, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve)
    model = build_chat_model(provider, preset["default_model"] or "administrator-model")
    if hasattr(model, "root_client"):
        model.root_client.max_retries = 0
    elif hasattr(model, "_client"):
        model._client.max_retries = 0
    assert observed == [], "Preparing a provider must not exchange tokens or start model work"
    assert model.bind_tools([catalog_echo]).invoke("Test").text == "OK"
    request = observed[-1]
    # Host/path checks are independent of successful parsing by a fake endpoint.
    if provider == "watsonx":
        assert request.url.path == "/ml/v1/text/chat"
        assert request.url.params["version"] == "2024-05-31"
        assert json.loads(request.content)["project_id"] == "test-project"
    elif provider == "sap-ai-core":
        assert request.url.path == "/v2/inference/deployments/test-deployment/chat/completions"
        assert request.headers["AI-Resource-Group"] == "default"
    elif provider != "gitlab":
        from novacode_cli.config.model_manager import provider_base_url

        expected = httpx.URL(provider_base_url(provider))
        override = preset.get("model_overrides", {}).get(preset["default_model"], {})
        if override.get("base_url") and override["base_url"] != preset["base_url"]:
            import os
            from string import Template

            expected = httpx.URL(Template(override["base_url"]).substitute(os.environ))
        assert request.url.host == expected.host
        assert request.url.path.startswith(expected.path.rstrip("/"))


@pytest.mark.usefixtures("catalog_environment")
def test_unavailable_tool_support_is_explicit(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.config.provider_auth import get_provider_auth_status

    monkeypatch.setenv("PERPLEXITY_API_KEY", "offline-key")
    assert not get_provider_auth_status("perplexity").is_usable
    with pytest.raises(ValueError, match="Sonar does not support function tools"):
        build_chat_model("perplexity", "sonar")


def test_required_account_settings_are_not_mistaken_for_api_keys(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.provider_auth import get_provider_auth_status

    monkeypatch.setenv("DATABRICKS_TOKEN", "offline-key")
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_BASE_URL", raising=False)
    status = get_provider_auth_status(
        "databricks", store=SimpleNamespace(has_key=lambda _var: True)
    )
    assert not status.is_usable
    assert "DATABRICKS_HOST" in status.detail


@pytest.mark.usefixtures("catalog_environment")
def test_regional_keys_are_independent_and_official_aliases_still_work(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.config.model_create import build_async_agent_model, build_chat_model
    from novacode_cli.config.model_manager import provider_api_key
    from novacode_cli.config.provider_auth import get_provider_auth_status

    monkeypatch.setenv("ALIBABA_API_KEY", "global-key")
    monkeypatch.setenv("ALIBABA_CN_API_KEY", "china-key")
    assert provider_api_key("alibaba") == "global-key"
    assert provider_api_key("alibaba-cn") == "china-key"
    monkeypatch.delenv("ALIBABA_API_KEY")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "official-alias-key")
    assert provider_api_key("alibaba") == "official-alias-key"
    assert get_provider_auth_status("alibaba").is_usable
    assert get_provider_auth_status("alibaba").env_var == "DASHSCOPE_API_KEY"
    model = build_chat_model("alibaba", CATALOG_PROVIDERS["alibaba"]["default_model"])
    assert model.openai_api_key.get_secret_value() == "official-alias-key"
    monkeypatch.setenv("ASYNC_AGENT_PROVIDER", "alibaba")
    monkeypatch.setenv("ASYNC_AGENT_MODEL", model.model_name)
    assert build_async_agent_model().openai_api_key.get_secret_value() == "official-alias-key"


@pytest.mark.parametrize("provider", ["deepseek", "infer", "minimax-cn", "gitlab", "watsonx"])
@pytest.mark.usefixtures("catalog_environment")
async def test_sdk_async_and_streaming_protocols(provider: str, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.model_create import build_chat_model

    preset = CATALOG_PROVIDERS[provider]
    monkeypatch.setenv(preset["api_key_var"], "offline-key")
    seen = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/identity/token"):
            return httpx.Response(200, json={"access_token": "managed-token"}, request=request)
        if request.url.path.endswith("/direct_access"):
            return httpx.Response(
                200, json={"token": "managed-token", "headers": {}}, request=request
            )
        payload = json.loads(request.content)
        assert payload["tools"]
        if request.url.path.endswith("/messages"):
            message = {
                "id": "msg-offline",
                "type": "message",
                "role": "assistant",
                "model": payload["model"],
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            events = [
                {
                    "type": "message_start",
                    "message": {**message, "content": [], "stop_reason": None},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "OK"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
        elif request.url.path.endswith("/responses"):
            item = {
                "type": "message",
                "id": "msg-offline",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}],
            }
            message = {
                "id": "resp-offline",
                "object": "response",
                "created_at": 1,
                "model": payload["model"],
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            events = [
                {
                    "type": "response.created",
                    "response": {**message, "status": "in_progress", "output": []},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "content": []},
                },
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "OK",
                    "item_id": "msg-offline",
                },
                {"type": "response.completed", "response": message},
            ]
        else:
            message = {
                "id": "chat-offline",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
            }
            events = [
                {
                    **message,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ]
        if not payload.get("stream"):
            return httpx.Response(200, json=message, request=request)
        body = "".join(
            f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
            for event in events
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body, request=request
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _self, req: serve(req))

    async def async_serve(_self: object, request: httpx.Request) -> httpx.Response:
        return serve(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_serve)
    model = build_chat_model(provider, preset["default_model"]).bind_tools([catalog_echo])
    assert (await model.ainvoke("Test")).text == "OK"
    assert "".join(chunk.text for chunk in model.stream("Test")) == "OK"
    assert "".join([chunk.text async for chunk in model.astream("Test")]) == "OK"
    model_requests = [
        req
        for req in seen
        if req.headers.get("content-type") == "application/json"
        and "tools" in json.loads(req.content)
    ]
    assert len(model_requests) == 3


@pytest.mark.usefixtures("catalog_environment")
async def test_vertex_uses_cloud_credentials_and_google_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
):
    import google.auth
    from google.oauth2.credentials import Credentials

    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.tools.fetch_tools import fetch_url

    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "offline-project")
    monkeypatch.setenv("GOOGLE_VERTEX_LOCATION", "global")
    monkeypatch.setenv("GOOGLE_API_KEY", "unrelated-developer-api-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda **_kw: (Credentials(token="adc-token"), "offline-project"),  # noqa: S106 — offline fixture
    )
    seen = []

    def serve(_self: object, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer adc-token"
        assert "offline-project" in request.url.path
        payload = json.loads(request.content)
        auth = payload["tools"][0]["functionDeclarations"][0]["parameters"]["properties"]["auth"]
        assert auth["items"]["type"] == "STRING"
        response = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}
            ]
        }
        return httpx.Response(200, json=response, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve)
    model = build_chat_model("google-vertex", CATALOG_PROVIDERS["google-vertex"]["default_model"])
    assert model.bind_tools([fetch_url]).invoke("Test").text == "OK"
    assert len(seen) == 1
    model.client.close()
    await model.client.aio.aclose()


def test_unusual_catalog_credentials_are_stripped_and_redacted(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.hooks import _sanitize_env_for_hook
    from novacode_cli.security.prompt_injection import _credential_values, _redact

    for variable in ("ABLIT_KEY", "CLARIFAI_PAT", "AICORE_SERVICE_KEY", "WATSONX_AI_APIKEY"):
        value = "offline-private-value-" + variable
        monkeypatch.setenv(variable, value)
        assert variable not in _sanitize_env_for_hook({variable: value})
        assert _redact(value, _credential_values()) == "[REDACTED]"


@pytest.mark.parametrize(
    ("provider", "variable"), [("parallel", "PARALLEL_API_KEY"), ("tinyfish", "TINYFISH_API_KEY")]
)
def test_tool_presets_resolve_current_credentials_without_saving_keys(
    provider: str,
    variable: str,
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.config import credentials
    from novacode_cli.mcp.client import build_mcp_server_config
    from novacode_cli.mcp.presets import create_config_from_preset

    monkeypatch.setattr(
        credentials, "credential_value", lambda name: "offline-key" if name == variable else ""
    )
    config = create_config_from_preset(provider)
    assert "offline-key" not in config.model_dump_json()
    assert build_mcp_server_config(config)["headers"]["Authorization"] == "Bearer offline-key"
    monkeypatch.setattr(credentials, "credential_value", lambda _name: "replacement-key")
    assert build_mcp_server_config(config)["headers"]["Authorization"] == "Bearer replacement-key"


def test_exa_keeps_anonymous_access_and_uses_an_optional_key(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config import credentials
    from novacode_cli.mcp.client import build_mcp_server_config
    from novacode_cli.mcp.presets import create_config_from_preset

    monkeypatch.setattr(credentials, "credential_value", lambda _name: "")
    config = create_config_from_preset("exa")
    assert not build_mcp_server_config(config)["headers"]
    monkeypatch.setattr(credentials, "credential_value", lambda _name: "exa-offline-key")
    assert build_mcp_server_config(config)["headers"]["x-api-key"] == "exa-offline-key"


@pytest.mark.parametrize(
    "reference", ["bearer:PATH", "optional:UNKNOWN_API_KEY", "bearer:PARALLEL_API_KEY\n"]
)
def test_mcp_credential_references_cannot_read_arbitrary_environment(reference: str):
    from novacode_cli.mcp.auth import resolve_header
    from novacode_cli.mcp.config import MCPServerConfig

    with pytest.raises(ValueError, match="credential reference"):
        MCPServerConfig(
            transport="http",
            url="https://example.test/mcp",
            header_secrets={"Authorization": reference},
        )
    with pytest.raises(ValueError, match="credential reference"):
        resolve_header(reference)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/mcp",
        "http://mcp.parallel.ai/mcp",
        "https://mcp.parallel.ai.evil.test/mcp",
        "https://mcp.parallel.ai/mcp/other",
    ],
)
def test_named_tool_credentials_cannot_be_redirected_by_configuration(url: str):
    from novacode_cli.mcp.auth import resolve_header
    from novacode_cli.mcp.config import MCPServerConfig

    with pytest.raises(ValueError, match="official endpoint"):
        MCPServerConfig(
            transport="http", url=url, header_secrets={"Authorization": "bearer:PARALLEL_API_KEY"}
        )
    with pytest.raises(ValueError, match="official endpoint"):
        resolve_header("bearer:PARALLEL_API_KEY", server_url=url, header_name="Authorization")


@pytest.mark.parametrize("provider", ["302ai", "wafer.ai", "minimax-cn"])
@pytest.mark.usefixtures("catalog_environment")
async def test_new_providers_restore_sessions_and_resolve_headless_models(
    provider: str, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.config.model_create import create_model_for_session
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.main import _resolve_cli_model

    preset = CATALOG_PROVIDERS[provider]
    monkeypatch.setenv(preset["api_key_var"], "offline-key")
    saved = NovaConfig().get_model_config()
    model, actual = await _resolve_cli_model(f"{provider}:{preset['default_model']}")
    assert actual == provider
    assert model.metadata["nova_provider"] == provider
    assert NovaConfig().get_model_config() == saved
    restored, warning = create_model_for_session(provider, preset["default_model"])
    assert warning is None
    assert restored.metadata["nova_provider"] == provider
