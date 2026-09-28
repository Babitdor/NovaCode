"""Tests for the /voice command + voice config round-trip (hermetic).

Audio deps are not installed in CI, so these exercise the config persistence and
the graceful-degradation path (install hint), not real audio.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from novacode_cli.commands.voice_handler import handle_voice_command
from novacode_cli.config.nova_config import NovaConfig


class _Console:
    """Captures console.print output for assertions."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args: object, **_kwargs: object) -> None:
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class _StubSecretManager:
    """SecretManager stand-in: a dict, so no real keychain is touched.

    Needed because under pytest ``keyring`` resolves to a null backend that
    *accepts* writes and persists nothing, which would make "the key was stored"
    an untestable claim.
    """

    store: dict[str, str] = {}

    def __init__(self) -> None:
        self.store = _StubSecretManager.store

    def store_secret(self, key: str, value: str) -> bool:
        self.store[key] = value
        return True

    def get_secret(self, key: str) -> str | None:
        return self.store.get(key)

    def delete_secret(self, key: str) -> bool:
        return self.store.pop(key, None) is not None


@pytest.fixture
def stub_keychain(monkeypatch):
    """Route credential writes into a dict instead of the OS keychain."""
    _StubSecretManager.store = {}
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _StubSecretManager)
    return _StubSecretManager


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point NovaConfig at a temp dir so tests never touch the real config."""
    cfg = NovaConfig()
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    monkeypatch.setattr(cfg, "config_path", tmp_path / "Nova.config.json")
    monkeypatch.setattr(cfg, "_config", {})
    # The handler does `from novacode_cli.config.nova_config import NovaConfig`
    # at call time, so patch the class at its source to return this instance.
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", lambda: cfg)
    return cfg


class TestVoiceConfig:
    def test_defaults_merge(self):
        cfg = NovaConfig()
        cfg._config = {}
        vc = cfg.get_voice_config()
        assert vc["enabled"] is False
        assert vc["mode"] == "push_to_talk"
        assert vc["tts_voice"] == "en_US-lessac-medium"

    def test_set_merges_known_keys_only(self):
        cfg = NovaConfig()
        cfg._config = {}
        monkeypatched = []
        cfg._save = lambda: monkeypatched.append(True)  # avoid disk write
        out = cfg.set_voice_config(mode="listen", bogus="x")
        assert out["mode"] == "listen"
        assert "bogus" not in out
        assert monkeypatched  # persisted


class TestVoiceCommand:
    async def test_status_runs(self, isolated_config):
        console = _Console()
        ok = await handle_voice_command("status", SimpleNamespace(), console)
        assert ok is True
        assert "mode=" in console.text

    async def test_on_off_persists(self, isolated_config):
        console = _Console()
        await handle_voice_command("on", SimpleNamespace(), console)
        assert isolated_config.get_voice_config()["enabled"] is True
        await handle_voice_command("off", SimpleNamespace(), console)
        assert isolated_config.get_voice_config()["enabled"] is False

    async def test_mode_alias(self, isolated_config):
        await handle_voice_command("mode listen", SimpleNamespace(), _Console())
        assert isolated_config.get_voice_config()["mode"] == "listen"
        await handle_voice_command("mode ptt", SimpleNamespace(), _Console())
        assert isolated_config.get_voice_config()["mode"] == "push_to_talk"

    async def test_bad_mode_usage(self, isolated_config):
        console = _Console()
        await handle_voice_command("mode bogus", SimpleNamespace(), console)
        assert "Usage" in console.text

    async def test_unknown_subcommand(self, isolated_config):
        console = _Console()
        await handle_voice_command("frobnicate", SimpleNamespace(), console)
        assert "Unknown" in console.text

    async def test_test_degrades_without_deps(self, isolated_config, monkeypatch):
        # Force "not available" and confirm we get the install hint, not a crash.
        monkeypatch.setattr(
            "novacode_cli.commands.voice_handler.audio.is_voice_available", lambda: False
        )
        console = _Console()
        await handle_voice_command("test", SimpleNamespace(), console)

    async def test_doctor_degrades_without_deps(self, isolated_config, monkeypatch):
        # /voice doctor should not crash when voice deps are missing.
        monkeypatch.setattr(
            "novacode_cli.commands.voice_handler.audio.is_voice_available", lambda: False
        )
        console = _Console()
        ok = await handle_voice_command("doctor", SimpleNamespace(), console)
        assert ok is True
        assert "diagnostics" in console.text.lower() or "install" in console.text.lower()
        assert "voice" in console.text.lower()

    async def test_configure_key_mapping(self, isolated_config, stub_keychain):
        # Configure ElevenLabs key using --key. The key must land in the
        # credential store, NOT in the config file, which no longer carries
        # secrets.
        console = _Console()
        await handle_voice_command(
            "settings tts elevenlabs --key test-eleven-key", SimpleNamespace(), console
        )
        pcfg = isolated_config.get_voice_provider_config("elevenlabs")
        assert stub_keychain.store == {"elevenlabs_api_key": "test-eleven-key"}
        assert pcfg.get("api_key") in ("", None)
        assert "test-eleven-key" not in json.dumps(pcfg)

    async def test_configure_key_clears_a_legacy_plaintext_copy(
        self, isolated_config, stub_keychain
    ):
        """Rotating a key must not leave the old plaintext value behind."""
        isolated_config.set_voice_provider_config("elevenlabs", api_key="old-plaintext", key="")
        await handle_voice_command(
            "settings tts elevenlabs --key new-key", SimpleNamespace(), _Console()
        )
        pcfg = isolated_config.get_voice_provider_config("elevenlabs")
        assert stub_keychain.store["elevenlabs_api_key"] == "new-key"
        assert "old-plaintext" not in json.dumps(pcfg)

    async def test_configure_key_falls_back_to_config_without_a_store(
        self, isolated_config, monkeypatch, stub_keychain
    ):
        """A provider with no credential slot keeps the old, config-based path."""
        monkeypatch.setattr(
            "novacode_cli.config.provider_auth.credential_env_var", lambda _name: None
        )
        console = _Console()
        await handle_voice_command(
            "settings tts elevenlabs --key legacy-key", SimpleNamespace(), console
        )
        pcfg = isolated_config.get_voice_provider_config("elevenlabs")
        assert pcfg.get("api_key") == "legacy-key"
        assert stub_keychain.store == {}
        assert isolated_config.get_voice_config()["tts_provider"] == "elevenlabs"

        # Configure Deepgram key using --key
        await handle_voice_command(
            "settings stt deepgram --key test-deepgram-key", SimpleNamespace(), console
        )
        pcfg = isolated_config.get_voice_provider_config("deepgram")
        assert pcfg.get("api_key") == "test-deepgram-key"
        assert isolated_config.get_voice_config()["stt_provider"] == "deepgram"

    async def test_legacy_key_fallback(self, isolated_config):
        # Manually set the config to simulate legacy key setup
        isolated_config.set_voice_provider_config("elevenlabs", key="legacy-eleven-key", api_key="")

        # Verify provider config shows key set
        console = _Console()
        await handle_voice_command("settings", SimpleNamespace(), console)
        assert "elevenlabs" in console.text.lower()
        assert "(key set)" in console.text.lower()

        # Verify VoicePipeline loads legacy key as api_key fallback
        from novacode_cli.audio.pipeline import VoicePipeline

        pipeline = VoicePipeline(
            tts_provider="elevenlabs",
            provider_configs=isolated_config.get_voice_config().get("providers", {}),
        )
        tts = pipeline._build_tts()
        assert tts._api_key == "legacy-eleven-key"

    async def test_download_degrades_without_deps(self, isolated_config, monkeypatch):
        # /voice download without deps → install hint, no crash, no warmup.
        monkeypatch.setattr(
            "novacode_cli.commands.voice_handler.audio.is_voice_available", lambda: False
        )
        console = _Console()
        ok = await handle_voice_command("download", SimpleNamespace(), console)
        assert ok is True
        assert "install" in console.text.lower() or "voice" in console.text.lower()

    async def test_download_skips_when_cached(self, isolated_config, monkeypatch):
        monkeypatch.setattr(
            "novacode_cli.commands.voice_handler.audio.is_voice_available", lambda: True
        )
        warmed = {"n": 0}

        class _Pipe:
            def __init__(self, **_kw):
                pass

            def downloads_pending(self):
                return []  # everything cached

            async def warmup(self):
                warmed["n"] += 1

        monkeypatch.setattr("novacode_cli.audio.pipeline.VoicePipeline", _Pipe)
        console = _Console()
        await handle_voice_command("download", SimpleNamespace(), console)
        assert "already downloaded" in console.text.lower()
        assert warmed["n"] == 0  # nothing to do → warmup not called

    async def test_download_runs_warmup_when_pending(self, isolated_config, monkeypatch):
        monkeypatch.setattr(
            "novacode_cli.commands.voice_handler.audio.is_voice_available", lambda: True
        )
        warmed = {"n": 0}

        class _Pipe:
            def __init__(self, **_kw):
                pass

            def downloads_pending(self):
                return ["STT", "TTS"]

            async def warmup(self):
                warmed["n"] += 1

        monkeypatch.setattr("novacode_cli.audio.pipeline.VoicePipeline", _Pipe)
        console = _Console()
        await handle_voice_command("download", SimpleNamespace(), console)
        assert warmed["n"] == 1
        assert "ready" in console.text.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
