"""Security and behavior tests for OpenAI ChatGPT-plan credentials."""

from __future__ import annotations

import json
import time
import urllib.parse

import pytest


class _SecretManager:
    values: dict[str, str] = {}

    def __init__(self):
        pass

    def get_secret(self, name: str):
        return self.values.get(name)

    def store_secret(self, name: str, value: str):
        self.values[name] = value
        return True

    def delete_secret(self, name: str):
        self.values.pop(name, None)
        return True


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch):
    from novacode_cli.config import openai_chatgpt_auth

    _SecretManager.values = {}
    with openai_chatgpt_auth._TOKEN_LOCK:
        openai_chatgpt_auth._TOKEN_RECORD = None
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _SecretManager)


def _record(**updates):
    from novacode_cli.config.openai_chatgpt_auth import _CREDENTIAL_NAME

    record = {
        "client_id": "oaiapp_test",
        "subject": "subject-1",
        "id_token": "id-token",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "scopes": ["chatgpt.tokens.use.direct"],
        "expires_at": time.time() + 3600,
    }
    record.update(updates)
    _SecretManager.values[_CREDENTIAL_NAME] = json.dumps(record)
    return record


def test_chatgpt_tokens_are_read_without_exporting_them(monkeypatch):
    from novacode_cli.config.openai_chatgpt_auth import access_token

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _record()

    assert access_token() == "access-token"
    assert "OPENAI_API_KEY" not in __import__("os").environ


def test_expired_access_token_is_refreshed_and_rotated(monkeypatch):
    from novacode_cli.config import openai_chatgpt_auth as auth

    _record(expires_at=0)
    seen = {}

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 1800,
                "scope": "chatgpt.tokens.use.direct openid",
            }

    def fake_post(url, *, data, timeout):
        seen.update(url=url, data=data, timeout=timeout)
        return _Response()

    monkeypatch.setattr(auth.httpx, "post", fake_post)

    assert auth.access_token() == "new-access"
    stored = auth.load_credentials()
    assert stored["access_token"] == "new-access"
    assert stored["refresh_token"] == "new-refresh"
    assert stored["expires_at"] > time.time() + 1700
    assert seen["data"]["grant_type"] == "refresh_token"
    assert "new-access" not in str(seen)


def test_sign_in_verifies_the_callback_and_saves_scoped_tokens(monkeypatch):
    from novacode_cli.config import openai_chatgpt_auth as auth

    state = {}

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "id_token": "verified-id-token",
                "access_token": "plan-access-token",
                "refresh_token": "plan-refresh-token",
                "expires_in": 3600,
                "scope": (
                    "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
                ),
            }

    def fake_post(url, *, data, timeout):
        state["exchange"] = data
        return _Response()

    def fake_open(url):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        state["authorize"] = params
        callback_url = params["redirect_uri"][0]
        callback_query = urllib.parse.urlencode(
            {
                "code": "one-time-code",
                "state": params["state"][0],
                "scope": (
                    "chatgpt.tokens.use.direct openid profile email offline_access resource.invoke"
                ),
                "client_id": "oaiapp_issued",
            }
        )
        import httpx

        with httpx.Client(trust_env=False) as client:
            client.get(f"{callback_url}?{callback_query}", timeout=5)
        return True

    monkeypatch.setattr(auth.httpx, "post", fake_post)
    monkeypatch.setattr(auth.webbrowser, "open", fake_open)
    monkeypatch.setattr(
        auth,
        "_verify_id_token",
        lambda token, *, client_id, nonce: {
            "sub": "verified-subject",
            "email": "user@example.com",
        },
    )

    record = auth.sign_in(timeout=5)

    assert state["authorize"]["client_id"] == ["dynamic_agent_client"]
    assert state["authorize"]["agent_name_hint"] == ["NovaCode"]
    assert state["authorize"]["scope"][0].find("chatgpt.tokens.use.direct") >= 0
    assert state["exchange"]["client_id"] == "oaiapp_issued"
    assert record["subject"] == "verified-subject"
    assert record["email"] == "user@example.com"
    assert record["access_token"] == "plan-access-token"
    assert auth.load_credentials() == record


def test_sign_in_rejects_missing_chatgpt_usage_permission(monkeypatch):
    from novacode_cli.config import openai_chatgpt_auth as auth

    state = {}

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "id_token": "id-token",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scope": "openid profile email offline_access resource.invoke",
            }

    def fake_post(url, *, data, timeout):
        state["exchange"] = data
        return _Response()

    def fake_open(url):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        state["authorize"] = params
        import httpx

        httpx.get(
            f"{params['redirect_uri'][0]}?"
            + urllib.parse.urlencode(
                {
                    "code": "one-time-code",
                    "state": params["state"][0],
                    "client_id": "oaiapp_test",
                    "scope": "openid profile email offline_access resource.invoke",
                }
            ),
            timeout=5,
        )
        return True

    monkeypatch.setattr(auth.httpx, "post", fake_post)
    monkeypatch.setattr(auth.webbrowser, "open", fake_open)
    monkeypatch.setattr(
        auth,
        "_verify_id_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Identity validation must not run without the required scope")
        ),
    )

    with pytest.raises(RuntimeError, match="did not grant ChatGPT plan usage"):
        auth.sign_in(timeout=3)
    assert state["authorize"]["client_id"] == ["dynamic_agent_client"]
    assert state["authorize"]["code_challenge_method"] == ["S256"]
    assert state["authorize"]["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert state["exchange"]["code_verifier"]
    assert auth.load_credentials() is None


def test_provider_status_accepts_chatgpt_auth_but_api_key_takes_precedence(monkeypatch):
    from novacode_cli.config.provider_auth import (
        ProviderAuthSource,
        get_provider_auth_status,
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _record()
    status = get_provider_auth_status("openai")
    assert status.is_usable
    assert status.source is ProviderAuthSource.STORED
    assert status.detail == "ChatGPT plan sign-in"

    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    status = get_provider_auth_status("openai")
    assert status.source is ProviderAuthSource.ENV
    assert status.detail == "set in environment: OPENAI_API_KEY"


def test_selected_chatgpt_auth_does_not_silently_use_an_api_key(monkeypatch):
    from novacode_cli.config.provider_auth import (
        ProviderAuthState,
        get_provider_auth_status,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setattr(
        "novacode_cli.config.openai_chatgpt_auth.selected_auth_mode",
        lambda: "chatgpt",
    )
    status = get_provider_auth_status("openai")
    assert status.state is ProviderAuthState.MISSING
    assert status.detail == "ChatGPT sign-in is selected but not connected"


def test_api_key_selection_does_not_accept_chatgpt_tokens_as_a_key(monkeypatch):
    from novacode_cli.config import model_create, openai_chatgpt_auth

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(model_create.settings, "openai_api_key", None, raising=False)
    monkeypatch.setattr(openai_chatgpt_auth, "has_credentials", lambda: True)
    monkeypatch.setattr(openai_chatgpt_auth, "selected_auth_mode", lambda: "api_key")

    class _NovaConfig:
        def get(self, key, default=None):
            return default

        def get_model_base_url(self):
            return None

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _NovaConfig)

    with pytest.raises(ValueError, match="no API key is configured"):
        model_create.build_chat_model("openai", "gpt-5-mini")


def test_openai_model_uses_responses_api_for_chatgpt_auth(monkeypatch):
    from novacode_cli.config import model_create, openai_chatgpt_auth

    monkeypatch.setenv("OPENAI_API_KEY", "api-key-must-not-win")
    monkeypatch.setattr(model_create.settings, "openai_api_key", None, raising=False)
    monkeypatch.setattr(openai_chatgpt_auth, "has_credentials", lambda: True)
    monkeypatch.setattr(openai_chatgpt_auth, "selected_auth_mode", lambda: "chatgpt")

    class _NovaConfig:
        def get(self, key, default=None):
            return default

        def get_model_base_url(self):
            return None

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", _NovaConfig)

    model = model_create.build_chat_model("openai", "gpt-5-mini")
    assert model.use_responses_api is True
    assert model.streaming is True
    assert model.store is False
    assert model.openai_api_key.get_secret_value() == "chatgpt-plan-token-managed-per-request"
    from langchain_core.messages import HumanMessage, SystemMessage

    payload = model._get_request_payload(
        [SystemMessage(content="system instructions"), HumanMessage(content="hello")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Run a shell command",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert payload["stream"] is True
    assert payload["store"] is False
    assert [message["role"] for message in payload["input"]] == ["developer", "user"]
    assert payload["tools"][0]["name"] == "shell"
