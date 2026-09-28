"""Credential storage: the secret in the keychain, metadata in the config.

The split matters. ``SecretManager`` holds one opaque string per name, so the
endpoint a personal key belongs to has nowhere to live; it is recorded in
``Nova.config.json`` instead. These tests pin both halves, and pin that the
secret itself never lands in that file.
"""

from __future__ import annotations

import json
import os

import pytest

from novacode_cli.config.credentials import (
    CredentialStore,
    apply_credential_to_env,
    credential_meta,
    delete_credential,
    get_stored_key,
    has_stored_credential,
    set_credential,
)

OPENAI = "OPENAI_API_KEY"


class _StubSecretManager:
    """SecretManager stand-in backed by a dict, with a switchable failure mode."""

    store: dict[str, str] = {}
    fail_writes = False
    deleted: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_secret(self, name: str) -> str | None:
        return type(self).store.get(name)

    def store_secret(self, name: str, value: str) -> bool:
        if type(self).fail_writes:
            return False
        type(self).store[name] = value
        return True

    def delete_secret(self, name: str) -> bool:
        type(self).deleted.append(name)
        type(self).store.pop(name, None)
        return True


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Keep config writes in a temp dir and secrets in a dict.

    Patching ``config.HOME_DIR`` (not just ``HOME``) is required: it is computed
    at import time and ``Settings.user_deepagents_dir`` returns that frozen
    constant, so a test that saves config would otherwise overwrite the
    developer's real ``Nova.config.json``.
    """
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _StubSecretManager)
    monkeypatch.delenv(OPENAI, raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _StubSecretManager.store = {}
    _StubSecretManager.fail_writes = False
    _StubSecretManager.deleted = []
    return tmp_path / ".nova"


def test_set_then_get_round_trips_under_the_env_var_name():
    outcome = set_credential(OPENAI, "sk-secret")
    assert outcome.ok
    assert get_stored_key(OPENAI) == "sk-secret"
    assert has_stored_credential(OPENAI)
    # Filed under the lowercased env var name: that is what `API_KEY_NAMES` and
    # `load_secrets_into_env` already use, so the keychain stays one namespace.
    assert _StubSecretManager.store == {"openai_api_key": "sk-secret"}


def test_a_saved_key_is_exported_to_the_environment():
    """Model construction reads os.environ, so a save has to reach it."""
    set_credential(OPENAI, "sk-secret")
    assert os.environ[OPENAI] == "sk-secret"


def test_reading_a_stored_key_does_not_export_it(monkeypatch):
    """Status checks call this on every provider; it must have no side effects."""
    _StubSecretManager.store = {"openai_api_key": "sk-secret"}
    monkeypatch.delenv(OPENAI, raising=False)

    assert get_stored_key(OPENAI) == "sk-secret"
    assert OPENAI not in os.environ


def test_the_secret_never_reaches_the_config_file(_isolated):
    set_credential(OPENAI, "sk-secret", base_url="http://localhost:1234/v1")

    raw = (_isolated / "Nova.config.json").read_text(encoding="utf-8")
    assert "sk-secret" not in raw
    assert "http://localhost:1234/v1" in raw


def test_endpoint_is_paired_with_the_key_and_cleared_when_dropped():
    set_credential(OPENAI, "sk-one", base_url="http://localhost:1234/v1")
    meta = credential_meta(OPENAI)
    assert meta is not None
    assert meta.base_url == "http://localhost:1234/v1"
    assert meta.added_at

    # A save with no endpoint clears the stored one, so a rotation cannot leave
    # the key pointing at an endpoint the user just removed.
    set_credential(OPENAI, "sk-two")
    assert credential_meta(OPENAI) is None


def test_stored_endpoint_is_exported_with_the_key(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    set_credential(OPENAI, "sk-one", base_url="http://localhost:1234/v1")
    assert os.environ["OPENAI_BASE_URL"] == "http://localhost:1234/v1"


def test_dropping_the_endpoint_clears_its_export(monkeypatch):
    set_credential(OPENAI, "sk-one", base_url="http://localhost:1234/v1")
    set_credential(OPENAI, "sk-two")

    assert "OPENAI_BASE_URL" not in os.environ


def test_delete_removes_the_secret_the_metadata_and_the_env_var():
    set_credential(OPENAI, "sk-one", base_url="http://localhost:1234/v1")

    assert delete_credential(OPENAI)

    assert _StubSecretManager.deleted == ["openai_api_key"]
    assert not has_stored_credential(OPENAI)
    assert credential_meta(OPENAI) is None
    assert OPENAI not in os.environ
    assert "OPENAI_BASE_URL" not in os.environ


def test_a_rejected_keychain_write_is_reported_not_raised():
    """The UI has to render this: a logger warning is invisible in a TUI."""
    _StubSecretManager.fail_writes = True

    outcome = set_credential(OPENAI, "sk-secret")

    assert not outcome.ok
    assert outcome.warnings
    assert OPENAI in outcome.warnings[0]
    assert "sk-secret" not in outcome.warnings[0]
    assert not has_stored_credential(OPENAI)


def test_an_empty_key_is_rejected_without_touching_the_store():
    outcome = set_credential(OPENAI, "   ")

    assert not outcome.ok
    assert _StubSecretManager.store == {}


def test_apply_is_a_no_op_when_nothing_is_stored():
    assert apply_credential_to_env(OPENAI) is None
    assert OPENAI not in os.environ


def test_metadata_is_listed_for_a_supplied_name_set():
    set_credential(OPENAI, "sk-one", base_url="http://localhost:1234/v1")

    listed = CredentialStore().list_meta([OPENAI, "ANTHROPIC_API_KEY"])

    # Only credentials that recorded something appear: a plain key writes no
    # metadata, so it never reaches the config file.
    assert set(listed) == {OPENAI}
    assert listed[OPENAI].base_url == "http://localhost:1234/v1"


def test_metadata_file_stays_valid_json_after_a_delete(_isolated):
    set_credential(OPENAI, "sk-one", base_url="http://localhost:1234/v1")
    delete_credential(OPENAI)

    data = json.loads((_isolated / "Nova.config.json").read_text(encoding="utf-8"))
    # The empty map is dropped entirely: `"credentials": {}` reads like
    # configuration the user set.
    assert "credentials" not in data
