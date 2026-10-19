"""Azure, Bedrock, Mistral, Groq, Cerebras, xAI, Hugging Face, Kimi For Coding, MiniMax."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

NEW = (
    "azure",
    "bedrock",
    "mistral",
    "groq",
    "cerebras",
    "xai",
    "huggingface",
    "kimi_coding",
    "minimax",
)
OPENAI_COMPATIBLE = {
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    "cerebras": ("CEREBRAS_API_KEY", "https://api.cerebras.ai/v1"),
    "xai": ("XAI_API_KEY", "https://api.x.ai/v1"),
    "huggingface": ("HF_TOKEN", "https://router.huggingface.co/v1"),
    "kimi_coding": ("KIMI_API_KEY", "https://api.kimi.com/coding/v1"),
}


@pytest.fixture(autouse=True)
def no_stored_keys(monkeypatch):
    """Keys come from the environment only; the real keychain is never read."""
    from novacode_cli.config import credentials

    monkeypatch.setattr(credentials, "get_stored_key", lambda env_var: None)
    monkeypatch.setattr(credentials, "credential_meta", lambda env_var: None)
    for var in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_RESOURCE_NAME",
        "KIMI_BASE_URL",
        "MINIMAX_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_every_new_provider_is_a_preset_with_a_credential():
    from novacode_cli.config.model_manager import MODEL_PRESETS
    from novacode_cli.config.provider_auth import credential_names

    names = credential_names()
    for provider in NEW:
        preset = MODEL_PRESETS[provider]
        assert preset["default_model"] in preset["models"]
        assert names[provider] == preset["api_key_var"]


@pytest.mark.parametrize("provider", sorted(OPENAI_COMPATIBLE))
def test_openai_compatible_providers_reach_their_own_url(monkeypatch, provider):
    from langchain_openai import ChatOpenAI

    from novacode_cli.config.model_create import _build_chat_model

    key_var, url = OPENAI_COMPATIBLE[provider]
    monkeypatch.setenv(key_var, f"{provider}-key")
    model = _build_chat_model(provider, "some-model")
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == url
    assert model.openai_api_key.get_secret_value() == f"{provider}-key"
    assert model.stream_usage is True
    payload = model._get_request_payload(
        [HumanMessage("hi", name="nova"), AIMessage("ok", name="x")]
    )
    assert all("name" not in m for m in payload["messages"])


def test_kimi_region_is_switched_by_its_base_url(monkeypatch):
    from novacode_cli.config.model_create import _build_chat_model

    monkeypatch.setenv("KIMI_API_KEY", "k")
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.kimi.ai/coding/v1/")
    assert _build_chat_model("kimi_coding", "kimi-for-coding").openai_api_base == (
        "https://api.kimi.ai/coding/v1"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://res.openai.azure.com",
        "https://res.openai.azure.com/",
        "https://res.openai.azure.com/openai/v1/",
        "https://res.openai.azure.com/openai",
    ],
)
def test_azure_uses_the_v1_route_of_any_endpoint_spelling(monkeypatch, endpoint):
    from novacode_cli.config.model_create import _build_chat_model

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    model = _build_chat_model("azure", "my-gpt-deployment")
    assert model.openai_api_base == "https://res.openai.azure.com/openai/v1"
    assert model.model_name == "my-gpt-deployment"


def test_azure_endpoint_from_resource_name_or_a_clear_error(monkeypatch):
    from novacode_cli.config.model_create import azure_base_url

    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        azure_base_url()
    monkeypatch.setenv("AZURE_RESOURCE_NAME", "myres")
    assert azure_base_url() == "https://myres.openai.azure.com/openai/v1"


def test_azure_endpoint_saved_in_auth_is_used(monkeypatch):
    from novacode_cli.config import credentials
    from novacode_cli.config.credentials import CredentialMeta
    from novacode_cli.config.model_create import azure_base_url

    monkeypatch.setattr(
        credentials,
        "credential_meta",
        lambda env_var: CredentialMeta(env_var, base_url="https://saved.openai.azure.com"),
    )
    assert azure_base_url() == "https://saved.openai.azure.com/openai/v1"


def test_minimax_uses_the_anthropic_messages_route(monkeypatch):
    from langchain_anthropic import ChatAnthropic

    from novacode_cli.config.model_create import _build_chat_model

    monkeypatch.setenv("MINIMAX_API_KEY", "mm")
    model = _build_chat_model("minimax", "MiniMax-M3")
    assert isinstance(model, ChatAnthropic)
    assert model.anthropic_api_url == "https://api.minimax.io/anthropic"
    assert model.anthropic_api_key.get_secret_value() == "mm"
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.cn/anthropic")
    assert _build_chat_model("minimax", "MiniMax-M3").anthropic_api_url == (
        "https://api.minimax.cn/anthropic"
    )


def test_bedrock_without_langchain_aws_says_how_to_install(monkeypatch):
    from novacode_cli.config.model_create import _build_chat_model

    monkeypatch.setitem(sys.modules, "langchain_aws", None)  # import fails
    with pytest.raises(RuntimeError, match="langchain-aws"):
        _build_chat_model("bedrock", "global.anthropic.claude-sonnet-5-5")


def test_bedrock_builds_converse_in_the_configured_region(monkeypatch):
    from novacode_cli.config.model_create import _build_chat_model

    seen = {}
    monkeypatch.setitem(
        sys.modules,
        "langchain_aws",
        SimpleNamespace(ChatBedrockConverse=lambda **kw: seen.update(kw) or "converse"),
    )
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert _build_chat_model("bedrock", "global.anthropic.claude-sonnet-5-5") == "converse"
    assert seen["model"] == "global.anthropic.claude-sonnet-5-5"
    assert seen["region_name"] == "eu-west-1"


def test_auth_status_accepts_other_names_for_the_credential(monkeypatch):
    from novacode_cli.config.provider_auth import ProviderAuthState, get_provider_auth_status

    store = SimpleNamespace(has_key=lambda env_var: False)
    for var in ("AZURE_OPENAI_API_KEY", "AZURE_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    assert get_provider_auth_status("bedrock", store=store).state is ProviderAuthState.MISSING
    monkeypatch.setenv("AWS_PROFILE", "work")
    assert get_provider_auth_status("bedrock", store=store).is_usable
    monkeypatch.setenv("AZURE_API_KEY", "az")
    assert get_provider_auth_status("azure", store=store).is_usable


def test_startup_copies_azure_api_key_to_the_name_nova_reads(monkeypatch):
    from novacode_cli import onboarding

    import os

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "")  # registered, so teardown restores it
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    monkeypatch.setenv("AZURE_API_KEY", "az")
    monkeypatch.setattr(onboarding, "SecretManager", lambda: (_ for _ in ()).throw(OSError()))
    onboarding.load_secrets_into_env()
    assert os.environ["AZURE_OPENAI_API_KEY"] == "az"


def test_model_lists_come_from_models_dev_newest_first(monkeypatch):
    from novacode_cli.config import model_catalog
    from novacode_cli.context import _models_dev

    catalog = {
        "groq": {
            "models": {
                "old": {"tool_call": True, "release_date": "2024-01-01"},
                "new": {"tool_call": True, "release_date": "2026-01-01"},
                "whisper": {"tool_call": False, "release_date": "2026-05-01"},
            }
        }
    }
    monkeypatch.setattr(_models_dev, "_load_catalog", lambda: catalog)
    model_catalog.clear_catalog_cache()
    models = model_catalog.get_provider_models("groq", refresh=True)
    assert models[:3] == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "llama-3.3-70b-versatile"]
    assert models[3:] == ["new", "old"]
    model_catalog.clear_catalog_cache()


def test_new_keys_never_reach_shell_subprocesses():
    from novacode_cli.shell import patterns

    names = next(
        v for v in vars(patterns).values() if isinstance(v, frozenset) and "NVIDIA_API_KEY" in v
    )
    for var in (
        "MISTRAL_API_KEY",
        "XAI_API_KEY",
        "HF_TOKEN",
        "KIMI_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
    ):
        assert var in names
