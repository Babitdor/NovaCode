"""Provider credential readiness: one answer per provider, with a reason.

A boolean is not enough for the UI — `/auth` badges and `/model` headers must
distinguish "no key stored", "set in your shell", "no key needed" and "unknown",
and the source matters because a key from the shell cannot be replaced from
inside the app.
"""

from __future__ import annotations

import pytest

from novacode_cli.config.provider_auth import (
    ProviderAuthSource,
    ProviderAuthState,
    credential_env_var,
    credential_names,
    get_all_auth_statuses,
    get_provider_auth_status,
    is_service,
    provider_display_name,
    service_env_var,
)


class _FakeStore:
    """Credential store stand-in: a set of env var names that have a key."""

    def __init__(self, stored: set[str] | None = None) -> None:
        self.stored = stored or set()

    def has_key(self, env_var: str) -> bool:
        return env_var in self.stored


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    """Strip any real key so the environment never decides a result."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    for preset in MODEL_PRESETS.values():
        var = preset.get("api_key_var")
        if var:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def test_an_unknown_name_is_unknown_not_missing():
    """Missing would claim a key is required; Nova does not know that here."""
    status = get_provider_auth_status("nonsense")

    assert status.state is ProviderAuthState.UNKNOWN
    assert status.as_legacy_bool() is None
    assert not status.is_usable


def test_a_keyless_provider_is_not_required():
    status = get_provider_auth_status("ollama")

    assert status.state is ProviderAuthState.NOT_REQUIRED
    assert status.env_var is None
    assert status.is_usable
    assert status.as_legacy_bool() is True


def test_a_stored_key_is_reported_as_stored():
    status = get_provider_auth_status("anthropic", store=_FakeStore({"ANTHROPIC_API_KEY"}))

    assert status.state is ProviderAuthState.CONFIGURED
    assert status.source is ProviderAuthSource.STORED
    assert status.env_var == "ANTHROPIC_API_KEY"
    assert status.is_usable


def test_an_environment_key_names_the_variable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")

    status = get_provider_auth_status("anthropic", store=_FakeStore())

    assert status.state is ProviderAuthState.CONFIGURED
    assert status.source is ProviderAuthSource.ENV
    # The badge shows this: a shell-supplied key cannot be replaced from /auth,
    # so the user needs to know which variable to change instead.
    assert "ANTHROPIC_API_KEY" in (status.detail or "")


def test_the_store_wins_over_the_environment(monkeypatch):
    """Both are configured, but only one of them /auth can manage."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")

    status = get_provider_auth_status("anthropic", store=_FakeStore({"ANTHROPIC_API_KEY"}))

    assert status.source is ProviderAuthSource.STORED


def test_an_empty_environment_variable_counts_as_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    status = get_provider_auth_status("anthropic", store=_FakeStore())

    assert status.state is ProviderAuthState.MISSING


def test_a_missing_key_names_what_is_missing():
    status = get_provider_auth_status("anthropic", store=_FakeStore())

    assert status.state is ProviderAuthState.MISSING
    assert status.env_var == "ANTHROPIC_API_KEY"
    assert status.detail == "ANTHROPIC_API_KEY is not set or is empty"
    assert status.as_legacy_bool() is False


def test_gateway_providers_are_checked_like_any_other():
    status = get_provider_auth_status("opencode", store=_FakeStore())

    assert status.state is ProviderAuthState.MISSING
    assert status.env_var == "OPENCODE_API_KEY"


def test_a_service_is_resolved_alongside_providers():
    assert is_service("tavily")
    assert service_env_var("tavily") == "TAVILY_API_KEY"

    status = get_provider_auth_status("tavily", store=_FakeStore({"TAVILY_API_KEY"}))

    assert status.state is ProviderAuthState.CONFIGURED
    assert status.source is ProviderAuthSource.STORED


def test_services_are_not_providers():
    assert not is_service("anthropic")
    assert service_env_var("anthropic") is None


def test_credential_names_exclude_keyless_providers():
    """Offering a key input for Ollama would be offering a field it ignores."""
    names = credential_names()

    assert "ollama" not in names
    assert names["anthropic"] == "ANTHROPIC_API_KEY"
    assert names["tavily"] == "TAVILY_API_KEY"
    assert names["deepgram"] == "DEEPGRAM_API_KEY"
    assert names["elevenlabs"] == "ELEVENLABS_API_KEY"
    assert set(names) == {
        "openai",
        "anthropic",
        "google",
        "openrouter",
        "opencode",
        "nvidia",
        "tavily",
        "deepgram",
        "elevenlabs",
    }


def test_env_var_lookup_covers_providers_and_services():
    assert credential_env_var("nvidia") == "NVIDIA_API_KEY"
    assert credential_env_var("tavily") == "TAVILY_API_KEY"
    assert credential_env_var("ollama") is None
    assert credential_env_var("nonsense") is None


def test_display_names_fall_back_to_the_id():
    assert provider_display_name("opencode") == "OpenCode Go"
    # Services have no preset to carry a name; showing the raw config key next
    # to "OpenAI" would read like a bug.
    assert provider_display_name("tavily") == "Tavily (web search)"
    assert provider_display_name("nonsense") == "nonsense"


def test_all_statuses_can_include_or_omit_keyless_providers():
    without = get_all_auth_statuses()
    with_keyless = get_all_auth_statuses(include_providers_without_keys=True)

    assert "ollama" not in without
    assert "ollama" in with_keyless
    assert set(with_keyless) >= set(without)


def test_a_keychain_only_key_makes_a_provider_available(monkeypatch):
    """The bug this module exists to fix.

    `get_available_providers` used to read `os.environ` alone. A key saved
    through /auth lives in the OS keychain and only reaches the environment
    during startup hydration, so a keychain-only install reported every provider
    as unavailable until the process was restarted with the key already there.
    """
    from novacode_cli.config import provider_auth

    monkeypatch.setattr(
        provider_auth,
        "get_all_auth_statuses",
        lambda **kwargs: {
            "openai": get_provider_auth_status("openai", store=_FakeStore({"OPENAI_API_KEY"})),
            "ollama": get_provider_auth_status("ollama", store=_FakeStore()),
        },
    )

    from novacode_cli.config.model_manager import ModelManager

    available = [provider for provider, _ in ModelManager().get_available_providers()]

    assert "openai" in available
