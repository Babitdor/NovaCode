"""Command Code as a provider: routing, key lookup, model list, /auth and errors."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage


@pytest.fixture
def keyed(monkeypatch):
    from novacode_cli.config import model_create

    monkeypatch.setattr(model_create.settings, "commandcode_api_key", "cmd-test")


def test_claude_models_go_to_the_messages_route(keyed):
    from langchain_anthropic import ChatAnthropic

    from novacode_cli.config.model_create import _build_chat_model

    model = _build_chat_model("commandcode", "claude-sonnet-5-5")
    assert isinstance(model, ChatAnthropic)
    # The SDK appends /v1/messages to this.
    assert model.anthropic_api_url == "https://api.commandcode.ai/provider"
    assert model.anthropic_api_key.get_secret_value() == "cmd-test"


def test_other_models_go_to_chat_completions_without_message_names(keyed):
    from langchain_openai import ChatOpenAI

    from novacode_cli.config.model_create import _build_chat_model

    model = _build_chat_model("commandcode", "zai-org/GLM-5.3")
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == "https://api.commandcode.ai/provider/v1"
    assert model.openai_api_key.get_secret_value() == "cmd-test"
    payload = model._get_request_payload([HumanMessage("hi", name="nova"), AIMessage("ok", name="x")])
    assert all("name" not in m for m in payload["messages"])


def test_the_key_is_read_from_either_documented_variable(monkeypatch):
    from novacode_cli.config.config import Settings

    monkeypatch.delenv("COMMANDCODE_API_KEY", raising=False)
    monkeypatch.setenv("CMD_API_KEY", "from-docs-name")
    settings = Settings.from_environment()
    assert settings.commandcode_api_key == "from-docs-name"
    assert settings.has_commandcode


def test_auth_lists_command_code_with_its_key():
    from novacode_cli.config.provider_auth import credential_env_var, provider_display_name

    assert credential_env_var("commandcode") == "COMMANDCODE_API_KEY"
    assert provider_display_name("commandcode") == "Command Code"


def test_live_model_list_keeps_claude_and_sends_no_opencode_header(monkeypatch):
    import httpx

    from novacode_cli.config.model_manager import get_opencode_models

    sent = {}

    def fake_get(url, headers, timeout):
        sent.update(url=url, headers=headers)
        data = [{"id": "claude-opus-5-5"}, {"id": "deepseek/deepseek-v4-flash"}]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": data})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert get_opencode_models("commandcode") == ["claude-opus-5-5", "deepseek/deepseek-v4-flash"]
    assert sent["url"] == "https://api.commandcode.ai/provider/v1/models"
    assert "x-opencode-session" not in sent["headers"]


def test_a_plan_without_api_access_is_named_and_not_retried():
    from novacode_cli.errors.provider_errors import friendly_model_error, is_retryable_model_error

    error = Exception("Error code: 403 - {'error': {'code': 'upgrade_required', 'type': 'x'}}")
    assert "plan has no API access" in friendly_model_error(error)
    assert not is_retryable_model_error(error)


def test_org_prefixed_ids_find_their_models_dev_window(monkeypatch):
    from novacode_cli.context import _models_dev

    catalog = {"zai": {"models": {"glm-5.3": {"limit": {"context": 1_000_000}}}}}
    monkeypatch.setattr(_models_dev, "_load_catalog", lambda: catalog)
    assert _models_dev.get_models_dev_context("zai-org/GLM-5.3") == 1_000_000


@pytest.mark.parametrize("model_name", ["claude-sonnet-5-5", "zai-org/GLM-5.3"])
@pytest.mark.parametrize("zdr", [True, False])
def test_zero_data_retention_header_follows_the_setting(keyed, monkeypatch, model_name, zdr):
    from novacode_cli.config.model_create import _build_chat_model
    from novacode_cli.config.nova_config import NovaConfig

    real_get = NovaConfig.get
    monkeypatch.setattr(
        NovaConfig, "get", lambda self, key, default=None: zdr if key == "commandcode_zdr"
        else real_get(self, key, default),
    )
    headers = _build_chat_model("commandcode", model_name).default_headers or {}
    assert (headers.get("x-cmd-zdr") == "1") is zdr


def test_jev_on_command_code_uses_only_the_command_code_key(monkeypatch):
    from novacode_cli.agents import tool_verdicts
    from novacode_cli.config import credentials
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr(credentials, "credential_value", lambda var: f"key:{var}")
    endpoint = NovaConfig.TOOL_VERDICT_COMMANDCODE_ENDPOINT
    assert tool_verdicts.decision_api_key(endpoint) == "key:COMMANDCODE_API_KEY"
    assert tool_verdicts.decision_api_key("http://localhost:1/v1/systemone") == "key:SYSTEM_ONE_API_KEY"


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 422 - {'error': {'code': 'cmd_zdr_no_providers'}}",
        "Error code: 422 - This model has no zero-data-retention upstream. "
        "Remove the x-cmd-zdr header or choose a different model.",
    ],
)
def test_no_zdr_route_is_explained_and_not_retried(message: str):
    from novacode_cli.errors.provider_errors import friendly_model_error, is_retryable_model_error

    error = Exception(message)
    assert "zero-data-retention" in friendly_model_error(error)
    assert not is_retryable_model_error(error)


def test_slash_models_opens_the_model_picker():
    from novacode_cli.tui.app import TUI_COMMANDS

    assert "models" in TUI_COMMANDS["model"].aliases
