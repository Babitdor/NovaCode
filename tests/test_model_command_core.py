"""/model command core — ModelManager is the shared seam for both UI adapters.

The console handler (commands/model_handler.py) and the native TUI path
(NovaApp._run_model + ModelScreen) must route through the same ModelManager
methods: availability, current-provider id, key resolution, and set_provider
persistence. Pins those behaviors plus a structural check that neither adapter
grows a private copy again.
"""

import inspect
import os
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# Voice picks from the same picker
#
# The picker carries a second axis, so a voice row must never reach the chat
# branch: `MODEL_PRESETS` has no entry for a voice provider id and would raise.
# ---------------------------------------------------------------------------

class _RecordingVoiceConfig:
    """NovaConfig stand-in that records the voice writes."""

    voice_calls: list[dict] = []
    provider_calls: list[tuple[str, dict]] = []
    fail = False

    def __init__(self, *a, **k) -> None:
        pass

    def set_voice_config(self, **updates):
        if type(self).fail:
            raise OSError("config is read-only")
        type(self).voice_calls.append(updates)
        return {}

    def set_voice_provider_config(self, provider, **updates):
        type(self).provider_calls.append((provider, updates))
        return {}


class _AppStub:
    """The bits of NovaApp `_apply_voice_pick` touches."""

    def __init__(self) -> None:
        self._voice_pipeline = "warmed-pipeline"
        self.session_state = SimpleNamespace(_voice_pipeline="warmed-pipeline")
        self.logged: list[str] = []

    def _log(self, text) -> None:
        self.logged.append(str(text))


@pytest.fixture
def voice_config(monkeypatch):
    _RecordingVoiceConfig.voice_calls = []
    _RecordingVoiceConfig.provider_calls = []
    _RecordingVoiceConfig.fail = False
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.NovaConfig", _RecordingVoiceConfig
    )
    return _RecordingVoiceConfig


def _apply(payload: dict) -> _AppStub:
    from novacode_cli.tui.app import NovaApp

    app = _AppStub()
    NovaApp._apply_voice_pick(app, payload)
    return app


def test_a_voice_pick_saves_the_field_and_activates_its_provider(voice_config):
    """Selecting a voice must also make its provider the active one.

    Otherwise picking "nova-3" while faster-whisper is active would save a value
    that nothing reads.
    """
    _apply(
        {
            "kind": "voice",
            "space": "stt",
            "provider": "deepgram",
            "field": "model",
            "model": "nova-3",
        }
    )

    assert voice_config.provider_calls == [("deepgram", {"model": "nova-3"})]
    assert voice_config.voice_calls == [{"stt_provider": "deepgram"}]


def test_a_voice_pick_writes_the_tll_fieldthe_payload_names(voice_config):
    """ElevenLabs stores a `voice_id`, not a `voice` — the payload decides."""
    _apply(
        {
            "kind": "voice",
            "space": "tts",
            "provider": "elevenlabs",
            "field": "voice_id",
            "model": "EXAVITQu4vr4xnSDxMaL",
        }
    )

    assert voice_config.provider_calls == [
        ("elevenlabs", {"voice_id": "EXAVITQu4vr4xnSDxMaL"})
    ]
    assert voice_config.voice_calls == [{"tts_provider": "elevenlabs"}]


def test_a_voice_pick_rebuilds_the_cached_pipeline(voice_config):
    """The pipeline is cached, so without this the pick silently does nothing.

    `_ensure_voice_pipeline` returns early while one exists, which is why
    `/voice settings` clears both references too.
    """
    app = _apply(
        {
            "kind": "voice",
            "space": "tts",
            "provider": "piper",
            "field": "voice",
            "model": "en_US-amy-medium",
        }
    )

    assert app._voice_pipeline is None
    assert app.session_state._voice_pipeline is None


def test_a_voice_pick_reports_what_changed(voice_config):
    app = _apply(
        {
            "kind": "voice",
            "space": "stt",
            "provider": "deepgram",
            "field": "model",
            "model": "nova-3",
        }
    )

    assert any("Deepgram" in line and "nova-3" in line for line in app.logged)


def test_an_empty_voice_name_is_ignored(voice_config):
    """A blank selection must not blank a working setting."""
    app = _apply({"kind": "voice", "space": "stt", "provider": "deepgram", "model": ""})

    assert voice_config.provider_calls == []
    assert voice_config.voice_calls == []
    assert app._voice_pipeline == "warmed-pipeline"


def test_a_config_failure_is_reported_not_raised(voice_config):
    voice_config.fail = True

    app = _apply(
        {
            "kind": "voice",
            "space": "stt",
            "provider": "deepgram",
            "field": "model",
            "model": "nova-3",
        }
    )

    assert any("Could not save" in line for line in app.logged)
    # The warmed pipeline stays: nothing changed, so nothing to rebuild.
    assert app._voice_pipeline == "warmed-pipeline"


def test_run_model_branches_on_kind_before_indexing_model_presets():
    """Ordering matters: `MODEL_PRESETS["deepgram"]` would raise KeyError.

    A voice provider id is absent from the chat presets, so the kind check has to
    come first.
    """
    from novacode_cli.tui.app import NovaApp

    src = inspect.getsource(NovaApp._run_model)
    branch = src.index('== "voice"')
    lookup = src.index("MODEL_PRESETS[provider]")

    assert branch < lookup
    assert "_apply_voice_pick(" in src
