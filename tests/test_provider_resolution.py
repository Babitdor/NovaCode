"""Endpoint and provider resolution: the inputs the /model screen and boot path trust.

``tests/test_model_endpoint.py`` covers the saved-endpoint flow end to end. This
file covers the resolver underneath it — which URL a preset resolves to, what a
bad endpoint is refused for, and how the current provider is decided when nothing
is saved — because a wrong endpoint silently sends a credential elsewhere and a
wrong fallback silently runs the user's session on a provider they did not pick.
"""

from __future__ import annotations

from types import SimpleNamespace


class _StubSettings:
    """Settings stand-in: every `has_<provider>` flag defaults to False."""

    def __init__(self, **flags: bool) -> None:
        self._flags = flags

    def __getattr__(self, name: str) -> bool:
        if name.startswith("has_"):
            return self._flags.get(name, False)
        message = f"unexpected setting {name}"
        raise AttributeError(message)


import pytest

from novacode_cli.config.model_manager import (
    MODEL_PRESETS,
    ModelManager,
    get_ollama_models,
    provider_base_url,
)


class _StubConfig:
    """NovaConfig stand-in exposing only the saved model selection."""

    def __init__(self, saved: dict[str, str] | None = None) -> None:
        self._saved = saved

    def get_model_config(self) -> dict[str, str] | None:
        return self._saved


def _manager(saved: dict[str, str] | None = None, **flags: bool) -> ModelManager:
    """A ModelManager with stubbed settings, so no real key decides the result."""
    manager = object.__new__(ModelManager)
    manager.settings = _StubSettings(**flags)
    manager.nova_config = _StubConfig(saved)
    manager.current_provider = None
    manager.current_model = None
    return manager


@pytest.fixture(autouse=True)
def _no_endpoint_env(monkeypatch) -> None:
    """A developer's own override must not decide these tests."""
    for preset in MODEL_PRESETS.values():
        var = preset.get("base_url_env")
        if var:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)


# --- provider_base_url -------------------------------------------------------


def test_the_preset_endpoint_is_the_default() -> None:
    assert provider_base_url("opencode") == MODEL_PRESETS["opencode"]["base_url"]


def test_a_base_url_env_override_wins(monkeypatch) -> None:
    """Self-hosted gateways, and the region variants, are switched this way."""
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.cn/anthropic")

    assert provider_base_url("minimax") == "https://api.minimax.cn/anthropic"


def test_a_per_model_override_endpoint_wins_over_the_preset() -> None:
    """One gateway can serve a model from a different host than its default."""
    assert provider_base_url("agentrouter", "claude-opus-5") == "https://agentrouter.org/v1"
    assert provider_base_url("agentrouter") == MODEL_PRESETS["agentrouter"]["base_url"]


def test_a_missing_endpoint_setting_is_named() -> None:
    """A raw KeyError from string.Template would not say which setting is missing."""
    with pytest.raises(ValueError, match="Databricks requires endpoint setting DATABRICKS_HOST"):
        provider_base_url("databricks")


def test_a_databricks_host_may_arrive_with_its_scheme(monkeypatch) -> None:
    """Users paste the workspace URL from the portal, scheme and trailing slash included."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-test.cloud.databricks.com/")

    assert (
        provider_base_url("databricks")
        == "https://dbc-test.cloud.databricks.com/ai-gateway/mlflow/v1"
    )


def test_an_endpoint_carrying_credentials_is_refused(monkeypatch) -> None:
    """A user:pass pair in the host would be logged and sent to the wrong party."""
    monkeypatch.setenv("DATABRICKS_HOST", "user:pass@dbc-test.cloud.databricks.com")

    with pytest.raises(ValueError, match=r"valid HTTP\(S\) endpoint without credentials") as caught:
        provider_base_url("databricks")

    assert "pass" not in str(caught.value).removeprefix("Databricks requires a valid HTTP(S)")


def test_an_endpoint_that_is_not_http_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAX_BASE_URL", "ftp://api.minimax.example/anthropic")

    with pytest.raises(ValueError, match=r"valid HTTP\(S\) endpoint"):
        provider_base_url("minimax")


def test_an_unknown_provider_is_a_key_error() -> None:
    """Pinned as-is: no production caller passes an unvalidated provider id (D2)."""
    with pytest.raises(KeyError):
        provider_base_url("nonsense")


# --- ollama model listing ----------------------------------------------------


def test_the_ollama_listing_is_parsed(monkeypatch) -> None:
    """Reality first: ask the local server what it serves, not the preset list."""

    def listing(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0, stdout="NAME        ID    SIZE   MODIFIED\nllama3:latest  a  4 GB  now\n"
        )

    monkeypatch.setattr("novacode_cli.config.model_manager.subprocess.run", listing)

    assert get_ollama_models() == ["llama3:latest"]


def test_a_missing_ollama_binary_falls_back_to_the_preset(monkeypatch) -> None:
    """Ollama not installed is normal: the preset list is the documented fallback."""

    def missing(*_args: object, **_kwargs: object) -> SimpleNamespace:
        message = "ollama is not on PATH"
        raise FileNotFoundError(message)

    monkeypatch.setattr("novacode_cli.config.model_manager.subprocess.run", missing)

    assert get_ollama_models() == MODEL_PRESETS["ollama"]["models"]


def test_a_header_only_listing_falls_back_to_the_preset(monkeypatch) -> None:
    """Zero installed models is not an empty model picker."""

    def empty(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="NAME  ID  SIZE  MODIFIED\n")

    monkeypatch.setattr("novacode_cli.config.model_manager.subprocess.run", empty)

    assert get_ollama_models() == MODEL_PRESETS["ollama"]["models"]


# --- which provider is current ----------------------------------------------


def test_a_saved_selection_decides_the_current_provider() -> None:
    manager = _manager({"provider": "anthropic", "model": "claude-sonnet-4-5-20250929"})

    assert manager.get_current_provider() == ("Anthropic", "claude-sonnet-4-5-20250929")


def test_a_saved_selection_for_an_unknown_provider_falls_through(monkeypatch) -> None:
    """A provider removed from the registry must not leave the session provider-less."""
    manager = _manager({"provider": "retired-gateway", "model": "old-model"}, has_anthropic=True)

    assert manager.get_current_provider() == ("Anthropic", "claude-sonnet-4-5-20250929")


def test_openai_wins_the_environment_fallback(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini")
    manager = _manager(has_openai=True, has_anthropic=True)

    assert manager.get_current_provider() == ("OpenAI", "gpt-5-mini")


def test_a_chatgpt_signin_counts_as_openai(monkeypatch) -> None:
    """No API key, but a plan sign-in: the session still runs on OpenAI."""
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr("novacode_cli.config.openai_chatgpt_auth.has_credentials", lambda: True)
    manager = _manager(has_anthropic=True)

    assert manager.get_current_provider() == ("OpenAI", "gpt-5-mini")


def test_a_failing_chatgpt_probe_does_not_break_the_fallback(monkeypatch) -> None:
    """A broken token store must degrade to the next provider, not raise."""

    def explode() -> bool:
        message = "token store is unreadable"
        raise RuntimeError(message)

    monkeypatch.setattr("novacode_cli.config.openai_chatgpt_auth.has_credentials", explode)
    manager = _manager(has_anthropic=True)

    assert manager.get_current_provider() == ("Anthropic", "claude-sonnet-4-5-20250929")


def test_google_is_reached_without_an_environment_key(monkeypatch) -> None:
    """Google is decided by the auth status, which is keychain-aware."""
    monkeypatch.delenv("GOOGLE_MODEL", raising=False)
    monkeypatch.setattr(
        "novacode_cli.config.provider_auth.get_provider_auth_status",
        lambda _name: SimpleNamespace(is_usable=True),
    )
    manager = _manager()

    assert manager.get_current_provider() == ("Google", "gemini-3-pro-preview")


def test_openrouter_is_the_last_environment_fallback(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(
        "novacode_cli.config.provider_auth.get_provider_auth_status",
        lambda _name: SimpleNamespace(is_usable=False),
    )
    manager = _manager(has_openrouter=True)

    assert manager.get_current_provider() == ("OpenRouter", "anthropic/claude-3.5-sonnet")


def test_no_credential_falls_back_to_local_ollama(monkeypatch) -> None:
    """Last resort: a keyless local server, so the session is never provider-less."""
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setattr(
        "novacode_cli.config.provider_auth.get_provider_auth_status",
        lambda _name: SimpleNamespace(is_usable=False),
    )
    monkeypatch.setattr("novacode_cli.config.openai_chatgpt_auth.has_credentials", lambda: False)

    provider, model = _manager().get_current_provider()  # type: ignore[misc]

    assert provider == "Ollama"
    assert model


def test_the_remaining_gateway_fallbacks_name_their_provider(monkeypatch) -> None:
    """Each of these has no saved selection, so its own flag decides the label."""
    monkeypatch.setattr(
        "novacode_cli.config.provider_auth.get_provider_auth_status",
        lambda _name: SimpleNamespace(is_usable=False),
    )
    monkeypatch.setattr(
        "novacode_cli.config.openai_chatgpt_auth.has_credentials", lambda: False
    )
    cases = [
        ("has_opencode", "OpenCode Go", "glm-5.3"),
        ("has_opencode_zen", "OpenCode Zen", "big-pickle"),
        ("has_commandcode", "Command Code", "deepseek/deepseek-v4-flash"),
        ("has_nvidia", "NVIDIA NIM", "deepseek-ai/deepseek-v4-pro-0813"),
    ]

    for flag, provider, default_model in cases:
        assert _manager(**{flag: True}).get_current_provider() == (provider, default_model)


def test_a_display_name_maps_back_to_its_provider_id() -> None:
    """set_provider and the pickers speak ids; get_current_provider speaks names."""
    manager = _manager({"provider": "nvidia", "model": "deepseek-ai/deepseek-v4-pro-0813"})

    assert manager.get_current_provider_id() == "nvidia"
    assert _manager().get_current_provider_id() == "ollama"


def test_a_credential_alias_is_read_when_the_primary_name_is_unset(monkeypatch) -> None:
    """Azure answers to AZURE_API_KEY too; /auth may have stored either name."""
    from novacode_cli.config.model_manager import provider_api_key

    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_API_KEY", "azure-alias-key")

    assert provider_api_key("azure") == "azure-alias-key"


def test_a_failed_gateway_listing_falls_back_to_the_preset(monkeypatch) -> None:
    """Offline or a gateway outage must still leave a usable model list."""
    from novacode_cli.config.model_manager import get_opencode_models

    def offline(*_args: object, **_kwargs: object) -> object:
        message = "gateway is down"
        raise OSError(message)

    monkeypatch.setattr("httpx.get", offline)

    assert get_opencode_models("opencode") == MODEL_PRESETS["opencode"]["models"]


def test_provider_statuses_include_providers_that_need_no_key() -> None:
    """The /model screen must be able to offer Ollama, which has no credential."""
    statuses = object.__new__(ModelManager).get_provider_statuses()

    assert "ollama" in statuses
    assert statuses["ollama"].is_usable
