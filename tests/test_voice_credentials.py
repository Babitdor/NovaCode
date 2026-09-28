"""Voice credentials: the store is the source, the config file is not.

The cloud voice backends used to read their key straight out of
``Nova.config.json``, which meant a secret sat in a plaintext file and a key
added through ``/auth`` was invisible to them. These tests pin the resolution
order (config field, then credential store, then environment), the startup
migration out of the old plaintext field, and that the pipeline is handed the
stored key.
"""

from __future__ import annotations

import json

import pytest

from novacode_cli.config.credentials import (
    credential_value,
    migrate_voice_keys,
    set_credential,
)

DEEPGRAM = "DEEPGRAM_API_KEY"
ELEVENLABS = "ELEVENLABS_API_KEY"


class _StubSecretManager:
    """SecretManager stand-in backed by a dict, so no real keychain is touched."""

    store: dict[str, str] = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_secret(self, name: str) -> str | None:
        return type(self).store.get(name)

    def store_secret(self, name: str, value: str) -> bool:
        type(self).store[name] = value
        return True

    def delete_secret(self, name: str) -> bool:
        type(self).store.pop(name, None)
        return True


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Temp config dir + dict-backed secrets; env vars restored afterwards."""
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _StubSecretManager)
    for name in (DEEPGRAM, ELEVENLABS):
        monkeypatch.delenv(name, raising=False)
    _StubSecretManager.store = {}
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _config():
    from novacode_cli.config.nova_config import NovaConfig

    return NovaConfig()


# ── credential_value: store first, then the environment ──────────────────────


def test_a_stored_key_wins_over_the_environment(monkeypatch):
    set_credential(DEEPGRAM, "from-keychain")
    monkeypatch.setenv(DEEPGRAM, "from-env")
    assert credential_value(DEEPGRAM) == "from-keychain"


def test_the_environment_is_used_when_nothing_is_stored(monkeypatch):
    monkeypatch.setenv(DEEPGRAM, "from-env")
    assert credential_value(DEEPGRAM) == "from-env"


def test_no_key_anywhere_is_an_empty_string(monkeypatch):
    monkeypatch.delenv(DEEPGRAM, raising=False)
    assert credential_value(DEEPGRAM) == ""


# ── Migration out of the plaintext config field ──────────────────────────────


def test_migration_moves_a_plaintext_key_into_the_store():
    """The old location is read, stored, then blanked — not merely ignored."""
    _config().set_voice_provider_config("elevenlabs", api_key="sk-plaintext")

    moved = migrate_voice_keys()

    assert moved == [ELEVENLABS]
    assert _StubSecretManager.store == {"elevenlabs_api_key": "sk-plaintext"}
    pcfg = _config().get_voice_provider_config("elevenlabs")
    assert pcfg.get("api_key") in ("", None)
    assert "sk-plaintext" not in json.dumps(pcfg)


def test_migration_also_clears_the_legacy_key_alias():
    _config().set_voice_provider_config("deepgram", key="legacy-shaped")

    assert migrate_voice_keys() == [DEEPGRAM]
    assert _StubSecretManager.store["deepgram_api_key"] == "legacy-shaped"
    assert _config().get_voice_provider_config("deepgram").get("key") in ("", None)


def test_migration_is_idempotent():
    _config().set_voice_provider_config("elevenlabs", api_key="sk-plaintext")

    assert migrate_voice_keys() == [ELEVENLABS]
    # Second run: nothing left to move, and the stored key is untouched.
    assert migrate_voice_keys() == []
    assert _StubSecretManager.store["elevenlabs_api_key"] == "sk-plaintext"


def test_migration_leaves_a_stored_key_alone_but_drops_the_copy():
    set_credential(ELEVENLABS, "the-real-key")
    _config().set_voice_provider_config("elevenlabs", api_key="stale-copy")

    assert migrate_voice_keys() == [ELEVENLABS]

    # The stored key is authoritative: the config copy must not overwrite it.
    assert _StubSecretManager.store["elevenlabs_api_key"] == "the-real-key"
    assert _config().get_voice_provider_config("elevenlabs").get("api_key") in ("", None)


def test_migration_does_nothing_when_there_is_no_plaintext_key():
    assert migrate_voice_keys() == []
    assert _StubSecretManager.store == {}


# ── The pipeline reads through all of it ─────────────────────────────────────


def test_build_stt_hands_deepgram_the_stored_key():
    """End to end: a key in the store reaches the backend with an empty config."""
    from novacode_cli.audio.pipeline import build_stt

    set_credential(DEEPGRAM, "dg-stored")

    backend = build_stt("deepgram", {"deepgram": {}})

    assert backend._api_key == "dg-stored"


def test_build_tts_hands_elevenlabs_the_stored_key():
    from novacode_cli.audio.pipeline import VoicePipeline

    set_credential(ELEVENLABS, "el-stored")

    built = VoicePipeline(tts_provider="elevenlabs", provider_configs={"elevenlabs": {}})

    assert built._build_tts()._api_key == "el-stored"


def test_a_config_value_still_wins_for_anyone_mid_migration(monkeypatch):
    """Ordering: a not-yet-migrated plaintext value is honored, then stored."""
    from novacode_cli.audio.pipeline import build_stt

    monkeypatch.setenv(DEEPGRAM, "from-env")
    backend = build_stt("deepgram", {"deepgram": {"api_key": "still-in-config"}})

    assert backend._api_key == "still-in-config"


def test_stt_from_voice_config_resolves_the_key_too(monkeypatch):
    """The remote voice-note path builds its backend the same way."""
    from novacode_cli.audio.pipeline import stt_from_voice_config

    set_credential(DEEPGRAM, "dg-stored")
    cfg = {
        "stt_provider": "deepgram",
        "providers": {"deepgram": {"model": "nova-2"}},
    }

    assert stt_from_voice_config(cfg)._api_key == "dg-stored"
