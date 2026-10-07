"""Executable auth specification (autonomous run; no separate spec approval).

Google Desktop OAuth must enforce official endpoints, PKCE and callback state,
cancel without saving, keep refresh tokens out of config/environment/logs, and
refresh bearer credentials for sync/async Gemini requests. API keys remain
independently selectable. Anthropic offers documented Console setup, not
Claude subscription-token collection. New dependencies: google-auth-oauthlib
for Google's OAuth flow; google-auth for refresh (already transitive packages).
Failure model: hostile client files, forged callbacks, stale tokens, credential
write failure, accidental API-key fallback, and late completion after dismissal.
"""

import json
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from novacode_cli.config import google_oauth_auth as auth
from tests.test_tui_sessions import isolated_session_config  # noqa: F401


class Secrets:
    values = {}

    def get_secret(self, name):
        return self.values.get(name)

    def store_secret(self, name, value):
        self.values[name] = value
        return True

    def delete_secret(self, name):
        self.values.pop(name, None)
        return True


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    Secrets.values = {}
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", Secrets)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def client_file(tmp_path, **updates):
    data = {
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "desktop-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    data.update(updates)
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"installed": data}))
    return path


def record(**updates):
    result = {
        "token": "access-secret",
        "refresh_token": "refresh-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "desktop-secret",
        "scopes": list(auth.SCOPES),
        "expiry": "2099-01-01T00:00:00Z",
        "quota_project_id": "test-project",
    }
    result.update(updates)
    return result


@pytest.mark.parametrize(
    "field,value",
    [
        ("auth_uri", "https://evil.example/auth"),
        ("token_uri", "https://evil.example/token"),
        ("client_id", "wrong-client"),
    ],
)
def test_hostile_client_configuration_rejected(tmp_path, field, value):
    with pytest.raises(ValueError, match="Google Desktop"):
        auth.read_client_config(client_file(tmp_path, **{field: value}))


def test_storage_selection_and_removal_preserve_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "existing-key")
    auth.save_credentials(record())
    auth.set_auth_mode("oauth")
    assert auth.selected_auth_mode() == "oauth"
    assert auth.load_credentials()["refresh_token"] == "refresh-secret"
    assert __import__("os").environ["GOOGLE_API_KEY"] == "existing-key"
    assert auth.delete_credentials()
    assert not auth.has_credentials()
    assert __import__("os").environ["GOOGLE_API_KEY"] == "existing-key"


def test_corrupt_or_redirected_saved_tokens_not_usable():
    Secrets.values[auth.CREDENTIAL_NAME] = json.dumps(record(token_uri="https://evil.example"))
    assert not auth.has_credentials()
    Secrets.values[auth.CREDENTIAL_NAME] = "bad JSON"
    assert not auth.has_credentials()


def test_failed_store_is_not_reported_as_connected(monkeypatch):
    monkeypatch.setattr(Secrets, "store_secret", lambda *_args: False)
    with pytest.raises(RuntimeError, match="save"):
        auth.save_credentials(record())
    assert not auth.has_credentials()


def test_real_loopback_state_pkce_and_no_implicit_save(tmp_path, monkeypatch):
    from google_auth_oauthlib.flow import InstalledAppFlow

    received = []

    def fetch(flow, **kwargs):
        received.append(kwargs)
        flow.oauth2session.token = {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": " ".join(auth.SCOPES),
        }
        return flow.oauth2session.token

    def browser(url):
        fields = parse_qs(urlsplit(url).query)
        assert fields["code_challenge_method"] == ["S256"]
        assert fields["access_type"] == ["offline"]
        callback = fields["redirect_uri"][0]

        def respond():
            with httpx.Client(trust_env=False) as client:
                forged = client.get(callback, params={"state": "forged", "code": "wrong"})
                assert forged.status_code == 400
                unicode_state = client.get(callback, params={"state": "漢字", "code": "wrong"})
                assert unicode_state.status_code == 400
                valid = client.get(
                    callback, params={"state": fields["state"][0], "code": "correct"}
                )
                assert valid.status_code == 200

        thread = threading.Thread(target=respond)
        thread.start()
        return True

    monkeypatch.setattr(InstalledAppFlow, "fetch_token", fetch)
    monkeypatch.setattr(auth.webbrowser, "open", browser)
    result = auth.sign_in(client_file(tmp_path), "test-project", threading.Event(), timeout=3)
    assert received[0]["code"] == "correct"
    assert result["quota_project_id"] == "test-project"
    assert result["refresh_token"] == "refresh-secret"
    assert not auth.has_credentials()


def test_cancel_and_timeout_do_not_store(tmp_path, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(auth.webbrowser, "open", lambda _url: True)
    with pytest.raises(RuntimeError, match="cancel"):
        auth.sign_in(client_file(tmp_path), "test-project", cancel, timeout=0.1)
    with pytest.raises(RuntimeError, match="timed out"):
        auth.sign_in(client_file(tmp_path), "test-project", threading.Event(), timeout=0.1)
    assert not auth.has_credentials()


@pytest.mark.asyncio
async def test_bearer_auth_sync_async_removes_api_key_and_blocks_other_hosts():
    auth.save_credentials(record())
    seen = []

    def serve(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer access-secret"
        assert request.headers["x-goog-user-project"] == "test-project"
        assert "x-goog-api-key" not in request.headers
        return httpx.Response(200)

    with httpx.Client(auth=auth.GoogleOAuthAuth(), transport=httpx.MockTransport(serve)) as client:
        client.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": "old"},
        )
        with pytest.raises(RuntimeError, match="endpoint"):
            client.get("https://evil.example/models")
    async with httpx.AsyncClient(
        auth=auth.GoogleOAuthAuth(), transport=httpx.MockTransport(serve)
    ) as client:
        await client.get("https://generativelanguage.googleapis.com/v1beta/models")
    assert len(seen) == 2


def test_expired_credentials_refresh_is_persisted(monkeypatch):
    from datetime import datetime

    from google.oauth2.credentials import Credentials

    auth.save_credentials(record(expiry="2000-01-01T00:00:00Z"))

    def refresh(credentials, _request):
        credentials.token = "new-token"
        credentials.expiry = datetime(2099, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", refresh)
    assert auth.access_token() == "new-token"
    assert auth.load_credentials()["token"] == "new-token"


@pytest.mark.asyncio
async def test_real_gemini_sdk_sync_async_and_streaming_use_bearer(monkeypatch):
    import langchain_google_genai  # noqa: F401 — load SDK before replacing HTTP factories

    from novacode_cli.config.model_create import build_chat_model

    auth.save_credentials(record())
    auth.set_auth_mode("oauth")
    sync_client, async_client = httpx.Client, httpx.AsyncClient
    seen = []
    response = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "Connected"}]}, "finishReason": "STOP"}
        ]
    }

    def serve(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer access-secret"
        assert request.headers["x-goog-user-project"] == "test-project"
        assert "x-goog-api-key" not in request.headers
        assert "key" not in request.url.params
        if "streamGenerateContent" in request.url.path:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(response) + "\n\n",
            )
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(httpx, "Client", lambda **kw: sync_client(**{**kw, "transport": transport}))
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: async_client(**{**kw, "transport": transport})
    )
    model = build_chat_model("google", "gemini-2.5-flash")
    assert model.invoke("Hi").content == "Connected"
    assert (await model.ainvoke("Hi")).content == "Connected"
    assert "".join(chunk.content for chunk in model.stream("Hi")) == "Connected"
    assert "".join([chunk.content async for chunk in model.astream("Hi")]) == "Connected"
    assert len(seen) == 4
    model.client.close()
    await model.client.aio.aclose()


def test_explicit_oauth_without_credentials_does_not_fall_back_to_api_key(monkeypatch):
    from novacode_cli.config.model_create import create_model_from_config
    from novacode_cli.config.provider_auth import get_provider_auth_status

    monkeypatch.setenv("GOOGLE_API_KEY", "valid-key")
    auth.set_auth_mode("oauth")
    assert not get_provider_auth_status("google").is_usable
    assert create_model_from_config("google", "gemini-2.5-flash") is None


def test_refresh_failure_missing_auth_and_invalid_records_fail_closed(monkeypatch):
    from google.oauth2.credentials import Credentials

    with pytest.raises(RuntimeError, match="missing"):
        auth.access_token()
    with pytest.raises(RuntimeError, match="usable"):
        auth.save_credentials(record(scopes=[]))
    with pytest.raises(ValueError, match="mode"):
        auth.set_auth_mode("unknown")
    auth.save_credentials(record(expiry="2000-01-01T00:00:00Z"))

    def failed(*_args):
        raise RuntimeError("refresh-secret")

    monkeypatch.setattr(Credentials, "refresh", failed)
    with pytest.raises(RuntimeError, match="refresh failed") as error:
        auth.access_token()
    assert "refresh-secret" not in str(error.value)
    monkeypatch.setattr(Secrets, "delete_secret", failed)
    assert not auth.delete_credentials()


def test_auth_modes_and_readiness_prioritize_selected_source(monkeypatch):
    from novacode_cli.config.provider_auth import get_provider_auth_status

    auth.save_credentials(record())
    assert auth.use_oauth(has_key=False)
    assert get_provider_auth_status("google").detail == "Google OAuth sign-in"
    assert not auth.use_oauth(has_key=True)
    auth.set_auth_mode("api_key")
    assert not auth.use_oauth(has_key=False)
    assert not get_provider_auth_status("google").is_usable
    monkeypatch.setenv("GEMINI_API_KEY", "alias-key")
    assert get_provider_auth_status("google").is_usable
    auth.set_auth_mode("oauth")
    assert auth.use_oauth(has_key=True)


def test_valid_client_file_roundtrip_and_hostile_records_property():
    from hypothesis import given
    from hypothesis import strategies as st

    @given(st.text())
    def rejected(uri):
        if uri != auth.TOKEN_URI:
            assert not auth._valid_record(record(token_uri=uri))

    rejected()


@pytest.mark.parametrize("reply", ["denied", "cancel", "no-refresh", "network"])
def test_browser_flow_failure_paths(tmp_path, monkeypatch, reply):
    from google_auth_oauthlib.flow import InstalledAppFlow

    cancel = threading.Event()

    def fetch(flow, **_kwargs):
        if reply == "network":
            raise OSError("transport-secret")
        flow.oauth2session.token = {
            "access_token": "access",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": " ".join(auth.SCOPES),
        }
        if reply == "cancel":
            cancel.set()
        return flow.oauth2session.token

    def browser(url):
        fields = parse_qs(urlsplit(url).query)
        params = {"state": fields["state"][0]}
        params.update({"error": "access_denied"} if reply == "denied" else {"code": "valid"})

        def callback():
            with httpx.Client(trust_env=False) as client:
                client.get(fields["redirect_uri"][0], params=params)

        threading.Thread(target=callback).start()
        return True

    monkeypatch.setattr(InstalledAppFlow, "fetch_token", fetch)
    monkeypatch.setattr(auth.webbrowser, "open", browser)
    with pytest.raises(RuntimeError) as error:
        auth.sign_in(client_file(tmp_path), "test-project", cancel, timeout=2)
    assert "transport-secret" not in str(error.value)
    assert not auth.has_credentials()


def test_bad_file_and_project_are_rejected_before_browser(tmp_path):
    with pytest.raises(ValueError, match="project ID"):
        auth.sign_in(tmp_path / "missing.json", "Invalid Project", threading.Event())
    with pytest.raises(ValueError, match="Google Desktop"):
        auth.read_client_config(tmp_path / "missing.json")
    path = client_file(tmp_path)
    assert auth.read_client_config(path)["installed"]["token_uri"] == auth.TOKEN_URI
    path.write_text("x" * 65537)
    with pytest.raises(ValueError, match="Google Desktop"):
        auth.read_client_config(path)
