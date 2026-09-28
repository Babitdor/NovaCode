"""The voice provider registry: keys, and the options the picker offers.

Two things have to stay true for a voice provider to be usable from the UI, and
neither is obvious from reading one file:

1. A provider that needs a key must be registered as a *service* in
   ``provider_auth`` and named in ``onboarding.API_KEY_NAMES``. The first is what
   puts it in ``/auth``; the second is what lets the startup hydration pass export
   it, and the cloud backends read the environment.
2. Every provider's ``option_field`` must be the config key its backend actually
   reads. ``model`` for STT, ``voice`` for most TTS, ``voice_id`` for ElevenLabs —
   writing the wrong one saves a value nothing ever looks at.
"""

from __future__ import annotations

from novacode_cli.audio.providers import STT_PROVIDERS, TTS_PROVIDERS
from novacode_cli.config.provider_auth import SERVICE_API_KEY_ENV, credential_env_var
from novacode_cli.onboarding import API_KEY_NAMES

#: The config key each voice provider's backend reads for its model/voice.
#: Pinned here so a backend rename fails this test instead of silently making a
#: picker selection a no-op.
_EXPECTED_FIELD = {
    "faster-whisper": "model",
    "deepgram": "model",
    "parakeet": "model",
    "piper": "voice",
    "elevenlabs": "voice_id",
    "orpheus": "voice",
    "pocket": "voice",
}

_ALL = {**STT_PROVIDERS, **TTS_PROVIDERS}


def test_every_cloud_voice_provider_can_be_authenticated_through_auth():
    for provider, meta in _ALL.items():
        if not meta.get("requires_key"):
            continue
        assert credential_env_var(provider) == SERVICE_API_KEY_ENV[provider], (
            f"{provider} needs a key but is not registered as a service, so it "
            "cannot be stored or deleted from /auth"
        )
        # And the startup hydration pass has to know the name, or a keychain-only
        # key never reaches the backend.
        assert API_KEY_NAMES[provider] == provider + "_api_key"


def test_local_providers_need_no_credential_slot():
    """A keyless provider in /auth would offer an input that does nothing."""
    for provider, meta in _ALL.items():
        if meta.get("requires_key"):
            continue
        assert provider not in SERVICE_API_KEY_ENV
        assert credential_env_var(provider) is None


def test_option_field_is_the_key_its_backend_reads():
    for provider, expected in _EXPECTED_FIELD.items():
        assert _ALL[provider]["option_field"] == expected, (
            f"{provider}'s picker would write the wrong config key"
        )


def test_every_provider_declares_its_options():
    for provider, meta in _ALL.items():
        assert isinstance(meta.get("options"), list), f"{provider} has no options list"
        assert meta.get("option_field"), f"{provider} has no option_field"


def test_the_default_is_always_selectable():
    """Otherwise the default is reachable only by typing it."""
    for provider, meta in STT_PROVIDERS.items():
        default = meta.get("default_model")
        if default is None:
            continue
        assert default in meta["options"], f"{provider}'s default is not offered"


def test_every_tts_provider_that_speaks_lists_its_default_voice():
    for provider, meta in TTS_PROVIDERS.items():
        default = meta.get("default_voice")
        if not default:  # "none" — silence has no voice
            continue
        assert default in meta["options"], f"{provider}'s default voice is not offered"


def test_off_is_the_only_tts_provider_with_no_options():
    """A regression guard: `none` must not start offering voices."""
    for provider, meta in TTS_PROVIDERS.items():
        if provider == "none":
            assert meta["options"] == []
        else:
            assert meta["options"], f"{provider} would render an empty section"


def test_labels_only_reference_values_that_exist():
    """A label for a value that is not offered is dead config."""
    for provider, meta in _ALL.items():
        labels = meta.get("option_labels") or {}
        unknown = set(labels) - set(meta["options"])
        assert not unknown, f"{provider} labels unknown value(s): {sorted(unknown)}"
