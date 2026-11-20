"""Credential-store and keychain failure behaviour: degrade, never crash, never leak.

The happy paths are pinned in ``tests/test_credentials.py``. This file covers the
branches a real machine hits — a locked keychain, a rejected write, a corrupt
config, a refused deletion — where a wrong answer is silent: a key that looks
saved and is not, a deletion that leaves the process still using the key, or an
exception raised into a TUI handler that cannot render a traceback.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from novacode_cli.config import credentials
from novacode_cli.config.credentials import CredentialStore

OPENAI = "OPENAI_API_KEY"
KEY = "sk-resilience-value"


class _StubSecretManager:
    """SecretManager stand-in with one switch per failure mode."""

    store: dict[str, str] = {}
    read_raises = False
    write_raises = False
    write_returns_false = False
    delete_raises = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def get_secret(self, name: str) -> str | None:
        if type(self).read_raises:
            message = f"keyring read failed for {name}"
            raise RuntimeError(message)
        return type(self).store.get(name)

    def store_secret(self, name: str, value: str) -> bool:
        if type(self).write_raises:
            message = f"keyring write failed for {name}"
            raise RuntimeError(message)
        if type(self).write_returns_false:
            return False
        type(self).store[name] = value
        return True

    def delete_secret(self, name: str) -> bool:
        if type(self).delete_raises:
            message = f"keyring delete failed for {name}"
            raise RuntimeError(message)
        type(self).store.pop(name, None)
        return True


class _StubNovaConfig:
    """Config stand-in for the voice-key migration failure branches."""

    voice: dict[str, dict[str, str]] = {}
    unreadable: set[str] = set()
    blank_raises = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def get_voice_provider_config(self, provider: str) -> dict[str, str]:
        if provider in type(self).unreadable:
            message = f"corrupt voice config for {provider}"
            raise RuntimeError(message)
        return dict(type(self).voice.get(provider, {}))

    def set_voice_provider_config(self, provider: str, **fields: str) -> bool:
        if type(self).blank_raises:
            message = "config write failed"
            raise RuntimeError(message)
        type(self).voice.setdefault(provider, {}).update(fields)
        return True


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Keep config writes in a temp dir and secrets in the dict-backed stub."""
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _StubSecretManager)
    monkeypatch.delenv(OPENAI, raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _StubSecretManager.store = {}
    _StubSecretManager.read_raises = False
    _StubSecretManager.write_raises = False
    _StubSecretManager.write_returns_false = False
    _StubSecretManager.delete_raises = False
    _StubNovaConfig.voice = {}
    _StubNovaConfig.unreadable = set()
    _StubNovaConfig.blank_raises = False
    return tmp_path / ".nova"


def test_a_failing_keychain_read_reports_no_credential() -> None:
    """A locked keychain must read as "nothing stored", not raise at the caller."""
    _StubSecretManager.read_raises = True

    assert CredentialStore().get_key(OPENAI) is None
    assert CredentialStore().has_key(OPENAI) is False


def test_a_failing_read_marks_the_store_unreadable() -> None:
    """D1: "not stored" and "could not read" must be distinguishable by a caller."""
    _StubSecretManager.read_raises = True
    store = CredentialStore()

    assert store.has_key(OPENAI) is False
    assert store.store_unreadable() is True


def test_a_healthy_store_says_it_can_answer() -> None:
    store = CredentialStore()

    assert store.has_key(OPENAI) is False
    assert store.store_unreadable() is False


def test_no_backend_at_all_counts_as_unreadable(monkeypatch) -> None:
    monkeypatch.setattr(credentials, "_default_manager", lambda: None)

    assert CredentialStore().store_unreadable() is True


def test_a_locked_keychain_reaches_the_status_as_an_unreadable_store() -> None:
    """End to end: the real store's failure becomes the badge's reason, not "unset"."""
    from novacode_cli.config.provider_auth import get_provider_auth_status

    _StubSecretManager.read_raises = True

    status = get_provider_auth_status("anthropic", store=CredentialStore())

    assert "could not read the credential store" in (status.detail or "")
    assert status.detail != "ANTHROPIC_API_KEY is not set or is empty"


def test_no_secret_backend_reports_an_unsaved_key(monkeypatch) -> None:
    """Without a backend there is nowhere to store it; say so instead of pretending."""
    monkeypatch.setattr(credentials, "_default_manager", lambda: None)

    outcome = CredentialStore().set_key(OPENAI, KEY)

    assert outcome.ok is False
    assert any("No credential store is available" in warning for warning in outcome.warnings)
    assert OPENAI not in os.environ
    assert KEY not in " ".join(outcome.warnings)


def test_a_failing_keychain_write_reports_and_keeps_the_secret_out() -> None:
    """The UI renders this; the message carries structure, never the key."""
    _StubSecretManager.write_raises = True

    outcome = CredentialStore().set_key(OPENAI, KEY)

    assert outcome.ok is False
    assert outcome.warnings
    assert KEY not in " ".join(outcome.warnings)
    assert OPENAI not in os.environ


def test_a_rejected_keychain_write_names_the_env_var() -> None:
    _StubSecretManager.write_returns_false = True

    outcome = CredentialStore().set_key(OPENAI, KEY)

    assert outcome.ok is False
    assert OPENAI in outcome.warnings[0]
    assert "rejected the write" in outcome.warnings[0]
    assert not CredentialStore().has_key(OPENAI)


def test_a_saved_but_unexportable_key_warns_and_stays_stored(monkeypatch) -> None:
    """Storage succeeded: the failure is the session export, and saying otherwise would be a lie."""

    def refuse(_self: CredentialStore, _env_var: str) -> str | None:
        message = "environment is immutable here"
        raise RuntimeError(message)

    monkeypatch.setattr(CredentialStore, "apply_to_env", refuse)

    outcome = CredentialStore().set_key(OPENAI, KEY)

    assert outcome.ok is True
    assert any("could not be applied to this session" in warning for warning in outcome.warnings)
    assert _StubSecretManager.store == {OPENAI.lower(): KEY}
    assert OPENAI not in os.environ


def test_a_delete_that_cannot_clear_metadata_still_reports_success(monkeypatch) -> None:
    """The secret is gone, which is what the return value promises."""

    class _RefusingDelete:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def set_credential_meta(self, *_args: object, **_kwargs: object) -> None:
            pass

        def delete_credential_meta(self, _env_var: str) -> None:
            message = "config is read-only"
            raise RuntimeError(message)

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _RefusingDelete)
    store = CredentialStore()
    assert store.set_key(OPENAI, KEY).ok

    assert store.delete(OPENAI) is True
    assert not store.has_key(OPENAI)
    assert OPENAI not in os.environ


def test_a_failing_delete_still_clears_metadata_and_environment() -> None:
    """The key may survive in the keychain, but the running process must stop using it."""
    store = CredentialStore()
    assert store.set_key(OPENAI, KEY, base_url="http://localhost:1234/v1").ok
    assert os.environ["OPENAI_BASE_URL"] == "http://localhost:1234/v1"
    _StubSecretManager.delete_raises = True

    assert store.delete(OPENAI) is False

    assert OPENAI not in os.environ
    assert "OPENAI_BASE_URL" not in os.environ
    assert store.meta(OPENAI) is None


def test_an_unreadable_config_yields_no_metadata(monkeypatch) -> None:
    """A corrupt config must not break a status pass over every provider."""

    class _Broken:
        def __init__(self, *args: object, **kwargs: object) -> None:
            message = "config is corrupt"
            raise RuntimeError(message)

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _Broken)

    assert CredentialStore().list_meta([OPENAI, "ANTHROPIC_API_KEY"]) == {}


def test_a_failed_metadata_write_is_reported_on_a_successful_save(monkeypatch) -> None:
    """A lost endpoint is a broken pairing, not a lost credential."""

    class _RefusingMeta:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def set_credential_meta(self, *_args: object, **_kwargs: object) -> None:
            message = "config is read-only"
            raise RuntimeError(message)

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _RefusingMeta)

    outcome = CredentialStore().set_key(OPENAI, KEY, base_url="http://localhost:1234/v1")

    assert outcome.ok is True
    assert any("endpoint could not be recorded" in warning for warning in outcome.warnings)
    assert KEY not in " ".join(outcome.warnings)
    assert _StubSecretManager.store == {OPENAI.lower(): KEY}


def test_a_missing_secret_backend_module_degrades_to_no_credentials(monkeypatch) -> None:
    """A keyring-less install: reads report nothing, writes warn, nothing raises."""
    monkeypatch.setitem(sys.modules, "novacode_cli.onboarding", None)

    store = CredentialStore()

    assert store.has_key(OPENAI) is False
    assert store.set_key(OPENAI, KEY).ok is False


def test_a_broken_secret_backend_degrades_to_no_credentials(monkeypatch) -> None:
    """Backend detection itself can fail; that is still not a crash."""

    class _Exploding:
        def __init__(self, *args: object, **kwargs: object) -> None:
            message = "no keyring backend"
            raise RuntimeError(message)

    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _Exploding)

    assert CredentialStore().has_key(OPENAI) is False
    assert CredentialStore().get_key(OPENAI) is None


# --- keychain read bounding (novacode_cli/config/keychain.py) ----------------


@pytest.fixture
def keychain_lock_reset():
    """Leave the module-level outstanding-read lock released for the next test."""
    from novacode_cli.config import keychain

    yield keychain
    assert keychain._pending.acquire(timeout=5)
    keychain._pending.release()


def test_a_keychain_read_that_outlives_the_timeout_raises(monkeypatch, keychain_lock_reset) -> None:
    keychain = keychain_lock_reset
    monkeypatch.setattr(keychain, "_READ_TIMEOUT", 0.2)

    def slow() -> int:
        time.sleep(0.6)
        return 1

    with pytest.raises(TimeoutError, match="did not respond"):
        keychain.keychain_read(slow)

    # The abandoned worker releases the lock when it finishes, so the next read
    # is not permanently blocked by one stuck keychain.
    time.sleep(0.6)
    assert keychain.keychain_read(lambda: 42) == 42


def test_a_keychain_worker_error_reaches_the_caller(keychain_lock_reset) -> None:
    """A read failure is the caller's to handle; swallowing it would hide a bad unlock."""
    keychain = keychain_lock_reset

    def explode() -> int:
        message = "keychain is locked"
        raise ValueError(message)

    with pytest.raises(ValueError, match="keychain is locked"):
        keychain.keychain_read(explode)


def test_a_keychain_read_that_cannot_start_releases_the_lock(monkeypatch, keychain_lock_reset) -> None:
    """Starting the worker can fail (no threads left); the lock must not stay held."""
    keychain = keychain_lock_reset

    class _NoThreads:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            message = "can't start new thread"
            raise RuntimeError(message)

    monkeypatch.setattr(keychain.threading, "Thread", _NoThreads)

    with pytest.raises(RuntimeError, match="can't start new thread"):
        keychain.keychain_read(lambda: 1)

    # The teardown assertion would fail if the lock were still held; assert early too.
    assert keychain._pending.acquire(timeout=2)
    keychain._pending.release()


def test_only_one_keychain_read_may_be_outstanding(keychain_lock_reset) -> None:
    """A second read fails fast instead of queueing behind a stuck one.

    The outstanding read is simulated by holding the module lock, so the test
    asserts the rule itself rather than a thread-scheduling coincidence.
    """
    keychain = keychain_lock_reset
    assert keychain._pending.acquire(timeout=2)
    try:
        with pytest.raises(TimeoutError, match="busy"):
            keychain.keychain_read(lambda: 2)
    finally:
        keychain._pending.release()


# --- voice key migration failure branches ------------------------------------


def test_migration_survives_an_unopenable_config(monkeypatch) -> None:
    from novacode_cli.config.credentials import migrate_voice_keys

    class _Broken:
        def __init__(self, *args: object, **kwargs: object) -> None:
            message = "config is corrupt"
            raise RuntimeError(message)

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _Broken)

    assert migrate_voice_keys() == []


def test_migration_skips_a_provider_whose_config_cannot_be_read(monkeypatch) -> None:
    """One corrupt section must not block the other provider's migration."""
    from novacode_cli.config.credentials import migrate_voice_keys

    _StubNovaConfig.voice = {
        "deepgram": {"api_key": "plaintext-deepgram"},
        "elevenlabs": {"api_key": "plaintext-elevenlabs"},
    }
    _StubNovaConfig.unreadable = {"deepgram"}
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _StubNovaConfig)

    assert migrate_voice_keys() == ["ELEVENLABS_API_KEY"]
    assert _StubNovaConfig.voice["elevenlabs"]["api_key"] == ""
    # The unreadable provider keeps its plaintext: better duplicated than lost.
    assert _StubNovaConfig.voice["deepgram"]["api_key"] == "plaintext-deepgram"


def test_migration_keeps_the_plaintext_when_the_store_rejects_the_key(monkeypatch) -> None:
    from novacode_cli.config.credentials import migrate_voice_keys

    _StubNovaConfig.voice = {"deepgram": {"api_key": "plaintext-deepgram"}}
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _StubNovaConfig)
    _StubSecretManager.write_returns_false = True

    assert migrate_voice_keys() == []

    # Blanking the config here would delete the only copy of the user's key.
    assert _StubNovaConfig.voice["deepgram"]["api_key"] == "plaintext-deepgram"


def test_migration_reports_nothing_when_the_config_cannot_be_blanked(monkeypatch) -> None:
    from novacode_cli.config.credentials import migrate_voice_keys

    _StubNovaConfig.voice = {"deepgram": {"api_key": "plaintext-deepgram"}}
    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _StubNovaConfig)
    _StubNovaConfig.blank_raises = True

    # Stored, but the copy still sits in the file: reporting it as migrated would
    # claim a secret no longer lives in plaintext when it still does.
    assert migrate_voice_keys() == []
    assert _StubSecretManager.store == {"deepgram_api_key": "plaintext-deepgram"}
