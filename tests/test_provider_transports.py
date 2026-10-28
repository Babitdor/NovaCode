"""Credential refresh, concurrency, and origin confinement at the HTTP boundary."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from novacode_cli.config.provider_transports import ProviderTokenAuth


def test_credentials_are_lazy_cached_and_confined(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def exchange(_transport: object, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "token": "fresh-token",
                "headers": {"x-gitlab-realm": "saas", "Host": "evil.test", "x-api-key": "evil-key"},
            },
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", exchange)
    auth = ProviderTokenAuth("gitlab", "private-key", "https://cloud.gitlab.com")
    assert not calls
    transport = httpx.MockTransport(lambda req: httpx.Response(200, request=req))
    with httpx.Client(auth=auth, transport=transport) as client:
        for _ in range(2):
            response = client.post(
                "https://cloud.gitlab.com/ai/v1/proxy/openai/v1/chat/completions", json={}
            )
            assert response.request.headers["Authorization"] == "Bearer fresh-token"
            assert response.request.headers["x-gitlab-realm"] == "saas"
            assert "x-api-key" not in response.request.headers
            assert response.request.headers["Host"] == "cloud.gitlab.com"
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer private-key"


@pytest.mark.parametrize(
    "url",
    ["https://evil.test/v1", "http://cloud.gitlab.com/v1", "https://cloud.gitlab.com:8443/v1"],
)
def test_wrong_origin_fails_before_refresh_or_request(url: str, monkeypatch: pytest.MonkeyPatch):
    def unexpected(_transport: object, _request: httpx.Request) -> httpx.Response:
        pytest.fail("A rejected origin must not start a credential exchange")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected)
    auth = ProviderTokenAuth("gitlab", "private-key", "https://cloud.gitlab.com")
    with (
        httpx.Client(
            auth=auth, transport=httpx.MockTransport(lambda _req: pytest.fail("request escaped"))
        ) as client,
        pytest.raises(RuntimeError, match="another origin"),
    ):
        client.get(url)


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {},
        {"token": ""},
        {"token": "secret\r\ninjected"},
        {"token": "secret", "headers": {"x-gitlab-test": "bad\nheader"}},
        {"token": "secret", "headers": []},
    ],
)
def test_bad_credentials_never_leak_response_body(reply: object, monkeypatch: pytest.MonkeyPatch):
    def exchange(_transport: object, request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(403, text="private-key server-private-message", request=request)
            if reply is None
            else httpx.Response(200, json=reply, request=request)
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", exchange)
    auth = ProviderTokenAuth("gitlab", "private-key", "https://cloud.gitlab.com")
    with pytest.raises(RuntimeError, match="credential exchange failed") as caught:
        next(auth.sync_auth_flow(httpx.Request("GET", "https://cloud.gitlab.com/")))
    assert "private-key" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert "server-private-message" not in str(caught.value)


async def test_async_refresh_and_expiry(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def exchange(_transport: object, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": "fresh-token", "expires_in": 120}, request=request
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", exchange)
    auth = ProviderTokenAuth("watsonx", "private-key", "https://us-south.ml.cloud.ibm.com/ml/v1")
    async with httpx.AsyncClient(
        auth=auth, transport=httpx.MockTransport(lambda req: httpx.Response(200, request=req))
    ) as client:
        for streaming in (False, True):
            response = await client.post(
                "https://us-south.ml.cloud.ibm.com/ml/v1/chat/completions?version=2024-05-31",
                json={"stream": streaming},
            )
            assert response.request.url.path.endswith(
                "/text/chat_stream" if streaming else "/text/chat"
            )
            assert response.request.url.params["version"] == "2024-05-31"
            auth._expires = 0
    assert len(calls) == 2


def test_concurrent_refresh_has_one_exchange(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def exchange(_transport: object, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": "fresh-token", "expires_in": 3600}, request=request
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", exchange)
    auth = ProviderTokenAuth("watsonx", "private-key", "https://us-south.ml.cloud.ibm.com")
    with ThreadPoolExecutor(max_workers=4) as pool:
        headers = list(pool.map(lambda _i: auth._load_headers(), range(4)))
    assert len(calls) == 1
    assert all(item["Authorization"] == "Bearer fresh-token" for item in headers)
