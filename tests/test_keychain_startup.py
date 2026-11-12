"""An unresponsive OS keychain cannot strand startup or the Python shutdown path."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from novacode_cli.config import keychain

if TYPE_CHECKING:
    from pathlib import Path


def test_stalled_read_is_bounded_and_does_not_accumulate_workers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(keychain, "_READ_TIMEOUT", 0.02)
    monkeypatch.setattr(keychain, "_pending", threading.Lock())
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def stalled() -> str:
        calls.append(threading.current_thread().daemon)
        try:
            release.wait(2)
            return "late secret"
        finally:
            finished.set()

    try:
        with pytest.raises(TimeoutError, match="did not respond"):
            keychain.keychain_read(stalled)
        with pytest.raises(TimeoutError, match="busy"):
            keychain.keychain_read(stalled)
        assert calls == [True]
    finally:
        release.set()
        assert finished.wait(1)


def test_read_preserves_values_and_backend_errors():
    assert keychain.keychain_read(lambda: "stored secret") == "stored secret"

    def fail() -> None:
        message = "locked backend"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="locked backend"):
        keychain.keychain_read(fail)


def test_settings_keep_environment_keys_when_backend_discovery_stalls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import keyring

    from novacode_cli.config.config import Settings

    def unavailable() -> None:
        message = "keychain unavailable"
        raise TimeoutError(message)

    monkeypatch.setattr(keyring, "get_keyring", unavailable)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    settings = Settings.from_environment(start_path=tmp_path)
    assert settings.openai_api_key == "environment-key"


def test_failed_read_lock_does_not_read_or_release_another_owners_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from novacode_cli.onboarding import SecretManager

    manager = object.__new__(SecretManager)
    manager.use_keyring = False
    manager.FALLBACK_FILE = tmp_path / "secrets.json"
    manager.FALLBACK_FILE.write_text('{"openai_api_key": "partial-write"}', encoding="utf-8")
    lock = manager.FALLBACK_FILE.with_suffix(".lock")
    lock.write_text("another owner", encoding="utf-8")
    monkeypatch.setattr("novacode_cli.onboarding._acquire_lock", lambda *_args, **_kwargs: False)

    assert manager.get_secret("openai_api_key") is None
    assert lock.read_text(encoding="utf-8") == "another owner"
