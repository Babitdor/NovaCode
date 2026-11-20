"""Provider auth status: the branches a badge depends on, including its failures.

``tests/test_provider_auth.py`` pins the resolution order and the source a key
came from. This file covers the remaining outcomes the ``/auth`` and ``/model``
screens read: which section a credential belongs to, a provider Nova knows is
limited, a provider missing a required setting, and a credential probe that
raises while the status must still render.
"""

from __future__ import annotations

import pytest

from novacode_cli.config.provider_auth import (
    ProviderAuthSource,
    ProviderAuthState,
    credential_section,
    get_provider_auth_status,
)


class _FakeStore:
    """Credential store stand-in: the env var names that have a stored key."""

    def __init__(self, stored: set[str] | None = None) -> None:
        self.stored = stored or set()

    def has_key(self, env_var: str) -> bool:
        return env_var in self.stored


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Keep saved config out of the developer's home and every key out of the env."""
    import novacode_cli.config.config as config_mod
    from novacode_cli.config.model_manager import MODEL_PRESETS
    from novacode_cli.config.provider_auth import SERVICE_API_KEY_ENV

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    for preset in MODEL_PRESETS.values():
        for var in (preset.get("api_key_var"), *preset.get("key_aliases", ())):
            if var:
                monkeypatch.delenv(var, raising=False)
    for var in SERVICE_API_KEY_ENV.values():
        monkeypatch.delenv(var, raising=False)
    for var in ("DATABRICKS_HOST", "GOOGLE_VERTEX_PROJECT"):
        monkeypatch.delenv(var, raising=False)


def test_credential_sections_group_the_auth_screen() -> None:
    """Each credential belongs to exactly one /auth section; a wrong section hides the field."""
    assert credential_section("deepgram") == "voice"
    assert credential_section("elevenlabs") == "voice"
    assert credential_section("jev") == "decisions"
    assert credential_section("systemone") == "decisions"
    assert credential_section("tavily") == "tools"
    assert credential_section("anthropic") == "llms"
    assert credential_section("ollama") == "llms"


def test_a_known_limitation_is_unknown_with_its_reason() -> None:
    """Nova ships the reason (Perplexity Sonar cannot call function tools)."""
    status = get_provider_auth_status("perplexity", store=_FakeStore())

    assert status.state is ProviderAuthState.UNKNOWN
    assert status.detail == (
        "Sonar does not support function tools. Use Perplexity Agent for coding tasks."
    )
    assert status.as_legacy_bool() is None
    assert not status.is_usable


def test_a_limited_provider_still_reports_where_its_key_came_from() -> None:
    """The state is UNKNOWN, but the source must not be lost: /auth can still replace it."""
    status = get_provider_auth_status("perplexity", store=_FakeStore({"PERPLEXITY_API_KEY"}))

    assert status.state is ProviderAuthState.UNKNOWN
    assert status.source is ProviderAuthSource.STORED
    assert status.env_var == "PERPLEXITY_API_KEY"


def test_a_provider_missing_a_required_setting_names_it() -> None:
    """Databricks needs a workspace host before its key means anything."""
    status = get_provider_auth_status("databricks", store=_FakeStore())

    assert status.state is ProviderAuthState.MISSING
    assert status.detail == "requires DATABRICKS_HOST"
    assert status.env_var == "DATABRICKS_TOKEN"
    assert status.as_legacy_bool() is False


def test_missing_settings_do_not_hide_a_stored_key() -> None:
    """The user must be able to see that the key itself is present and only the host is missing."""
    status = get_provider_auth_status("databricks", store=_FakeStore({"DATABRICKS_TOKEN"}))

    assert status.state is ProviderAuthState.MISSING
    assert status.source is ProviderAuthSource.STORED


def test_missing_settings_report_an_environment_key_too(monkeypatch) -> None:
    monkeypatch.setenv("DATABRICKS_TOKEN", "env-token")

    status = get_provider_auth_status("databricks", store=_FakeStore())

    assert status.state is ProviderAuthState.MISSING
    assert status.source is ProviderAuthSource.ENV


def test_a_satisfied_required_setting_leaves_the_key_check_to_the_store(monkeypatch) -> None:
    """With the host set, readiness is decided by the credential alone."""
    monkeypatch.setenv("DATABRICKS_HOST", "dbc-test.cloud.databricks.com")

    without_key = get_provider_auth_status("databricks", store=_FakeStore())
    assert without_key.state is ProviderAuthState.MISSING

    configured = get_provider_auth_status("databricks", store=_FakeStore({"DATABRICKS_TOKEN"}))
    assert configured.state is ProviderAuthState.CONFIGURED
    assert configured.source is ProviderAuthSource.STORED


class _UnreadableStore:
    """Credential store whose keychain read fails: locked, absent, or broken."""

    def has_key(self, env_var: str) -> bool:
        return False

    def get_key(self, env_var: str) -> None:
        return None

    def store_unreadable(self) -> bool:
        return True


def test_a_vertex_provider_reports_managed_cloud_credentials(monkeypatch) -> None:
    """Google Vertex authenticates through ADC: Nova supplies no key, and must say so."""
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "my-project")

    status = get_provider_auth_status("google-vertex")

    assert status.state is ProviderAuthState.MANAGED
    assert status.is_usable
    assert status.as_legacy_bool() is True
    assert status.detail == "Application Default Credentials (Google Cloud)"


def test_a_provider_requiring_a_key_but_naming_no_variable_is_unknown(monkeypatch) -> None:
    """The registry can hold a provider whose key source Nova does not know.

    Claiming MISSING there would tell the user to set a variable Nova cannot
    name; UNKNOWN is the honest answer.
    """
    from novacode_cli.config import provider_auth

    synthetic = dict(provider_auth._presets())
    synthetic["synthetic"] = {"name": "Synthetic", "requires_api_key": True}
    monkeypatch.setattr(provider_auth, "_presets", lambda: synthetic)

    status = get_provider_auth_status("synthetic")

    assert status.state is ProviderAuthState.UNKNOWN
    assert status.env_var is None
    assert status.detail == "credentials unknown"
    assert status.as_legacy_bool() is None


def test_a_locked_credential_store_is_not_reported_as_a_missing_key() -> None:
    """A keychain that cannot be read is not the user's configuration being wrong.

    D1: the badge said "ANTHROPIC_API_KEY is not set or is empty" while the key
    was stored and the keyring was merely locked — advice that sends the user to
    re-enter a key that is already there.
    """
    status = get_provider_auth_status("anthropic", store=_UnreadableStore())

    assert status.state is ProviderAuthState.MISSING
    assert status.detail is not None
    assert "could not read the credential store" in status.detail
    assert status.detail != "ANTHROPIC_API_KEY is not set or is empty"


def test_a_readable_store_keeps_the_precise_missing_key_message() -> None:
    """The obvious message must survive for the case it describes."""
    status = get_provider_auth_status("anthropic", store=_FakeStore())

    assert status.detail == "ANTHROPIC_API_KEY is not set or is empty"


def test_an_environment_key_still_wins_over_an_unreadable_store(monkeypatch) -> None:
    """A key in the shell is usable no matter what the keychain is doing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")

    status = get_provider_auth_status("anthropic", store=_UnreadableStore())

    assert status.state is ProviderAuthState.CONFIGURED
    assert status.source is ProviderAuthSource.ENV


def test_a_failing_chatgpt_credential_probe_does_not_break_the_status(monkeypatch) -> None:
    """Auth status is read on every screen render; it may never raise."""

    def explode() -> bool:
        message = "token store is unreadable"
        raise RuntimeError(message)

    monkeypatch.setattr("novacode_cli.config.openai_chatgpt_auth.has_credentials", explode)

    status = get_provider_auth_status("openai", store=_FakeStore())

    assert status.state is ProviderAuthState.MISSING
    assert status.env_var == "OPENAI_API_KEY"
    assert status.as_legacy_bool() is False
