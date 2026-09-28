"""/model command core — ModelManager is the shared seam for both UI adapters.

The console handler (commands/model_handler.py) and the native TUI path
(NovaApp._run_model + ModelScreen) must route through the same ModelManager
methods: availability, current-provider id, key resolution, and set_provider
persistence. Pins those behaviors plus a structural check that neither adapter
grows a private copy again.
"""

import inspect
import os

import pytest

from novacode_cli.config.model_manager import MODEL_PRESETS, ModelManager

KEY_VARS = [p["api_key_var"] for p in MODEL_PRESETS.values() if p["api_key_var"]]


class _RecordingNovaConfig:
    """NovaConfig stand-in: records set_model_config, serves a canned config."""

    saved = None
    model_config = None

    def __init__(self, *a, **k):
        pass

    def get_model_config(self):
        return type(self).model_config

    def set_model_config(self, provider, model, base_url=None):
        type(self).saved = (provider, model)

    def get_credential_meta(self):
        return {}

    def get_recent_models(self):
        return []

    def get(self, key, default=None):
        return default


class _StubSecrets:
    """SecretManager stand-in backed by a plain dict."""

    store: dict = {}

    def __init__(self, *a, **k):
        pass

    def get_secret(self, name):
        return type(self).store.get(name)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    _RecordingNovaConfig.saved = None
    _RecordingNovaConfig.model_config = None
    _StubSecrets.store = {}
    monkeypatch.setattr(
        "novacode_cli.config.model_manager.NovaConfig", _RecordingNovaConfig
    )
    # Credential metadata is read through its own module-level name, so the
    # stub has to be installed there too or credential reads hit the real
    # ~/.nova/Nova.config.json.
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig", _RecordingNovaConfig
    )
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _StubSecrets)
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    for preset in MODEL_PRESETS.values():
        monkeypatch.delenv(preset["env_var"], raising=False)


# ---------------------------------------------------------------------------
# Provider availability respects env keys
# ---------------------------------------------------------------------------

def test_availability_without_keys_is_ollama_only():
    available = [pid for pid, _ in ModelManager().get_available_providers()]
    assert available == ["ollama"]


def test_availability_respects_env_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    available = {pid for pid, _ in ModelManager().get_available_providers()}
    assert "openai" in available
    assert "anthropic" not in available


def test_availability_counts_a_key_stored_in_the_keychain():
    """The regression this list exists to catch.

    Availability used to read `os.environ` alone, so a key entered through the
    TUI (which lands in the OS keychain, and only reaches the environment during
    startup hydration) reported its provider as unavailable for the rest of the
    session — the picker kept saying "(needs key)" for a provider that had one.
    """
    _StubSecrets.store = {"openai_api_key": "kc-key"}

    available = {pid for pid, _ in ModelManager().get_available_providers()}

    assert "openai" in available
    assert "anthropic" not in available


# ---------------------------------------------------------------------------
# Switch persistence
# ---------------------------------------------------------------------------

def test_set_provider_persists_config_and_env():
    ModelManager().set_provider("anthropic")
    default = MODEL_PRESETS["anthropic"]["default_model"]
    assert _RecordingNovaConfig.saved == ("anthropic", default)
    assert os.environ["ANTHROPIC_MODEL"] == default


def test_set_provider_unknown_raises():
    with pytest.raises(ValueError):
        ModelManager().set_provider("nonsense")


# ---------------------------------------------------------------------------
# get_current_provider_id (reverse name -> id lookup, shared by both UIs)
# ---------------------------------------------------------------------------

def test_current_provider_id_from_saved_config():
    _RecordingNovaConfig.model_config = {"provider": "google", "model": "g"}
    assert ModelManager().get_current_provider_id() == "google"


def test_current_provider_id_defaults_to_ollama():
    assert ModelManager().get_current_provider_id() == "ollama"


# ---------------------------------------------------------------------------
# resolve_api_key (keychain-or-env, shared by both UIs)
# ---------------------------------------------------------------------------

def test_resolve_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert ModelManager().resolve_api_key("openai") == "env-key"


def test_resolve_api_key_from_keychain_exports_env():
    _StubSecrets.store = {"openai_api_key": "kc-key"}
    assert ModelManager().resolve_api_key("openai") == "kc-key"
    assert os.environ["OPENAI_API_KEY"] == "kc-key"


def test_resolve_api_key_missing_returns_none():
    assert ModelManager().resolve_api_key("openai") is None
    assert ModelManager().resolve_api_key("ollama") is None  # keyless provider


# ---------------------------------------------------------------------------
# Both adapters route through the shared core (structural)
# ---------------------------------------------------------------------------

def test_the_tui_is_the_only_model_adapter():
    """Model switching routes through ModelManager's shared core.

    The console ``/model`` adapter was removed with the REPL, so the TUI's
    ``_run_model`` is now the single adapter. It must still go through the
    shared ModelManager API rather than reimplementing provider handling.
    """
    from novacode_cli.tui.app import NovaApp

    tui_src = inspect.getsource(NovaApp._run_model)
    assert ".resolve_api_key(" in tui_src
    assert ".set_provider(" in tui_src
    assert ".get_current_provider_id(" in tui_src
    assert "ModelScreen(" in tui_src


def test_availability_is_reported_by_the_auth_path():
    """`get_available_providers` is the readiness summary `/auth` prints.

    The assertion moved off ``_run_model`` when the picker stopped deciding what
    to show from a precomputed availability set: it renders every provider with
    its own indicator and routes a credential-less pick into ``/auth``.
    """
    from novacode_cli.tui.app import NovaApp

    assert ".get_available_providers(" in inspect.getsource(NovaApp._run_auth)


def test_console_model_handler_points_at_the_tui():
    """The leftover console /model entry must not try to prompt (no REPL)."""
    from novacode_cli.commands import model_handler

    src = inspect.getsource(model_handler)
    assert "import PromptSession" not in src
    assert "run_interactive_menu" not in src
    assert "prompt_async" not in src
