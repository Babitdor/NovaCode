"""Managed-transport guards: what must be refused before a secret is used.

``tests/test_provider_transports.py`` covers refresh, caching, concurrency and
origin confinement end to end. This file covers the guards that run first: the
endpoint validator, and the SAP AI Core service key that decides whether a
transport is built at all. A guard that accepts bad input sends a credential to
an endpoint it was never meant for, so each rejection is pinned with the message
the user sees — and with what that message must not contain.
"""

from __future__ import annotations

import json

import pytest

from novacode_cli.config.provider_transports import (
    ProviderTokenAuth,
    _https_url,
    build_managed_model,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://cloud.gitlab.com",
        "cloud.gitlab.com/path",
        "https://user:pass@cloud.gitlab.com",
        "https://cloud.gitlab.com?token=leaked",
        "https://cloud.gitlab.com#fragment",
        "",
    ],
)
def test_only_a_plain_https_origin_is_an_acceptable_endpoint(url: str) -> None:
    """A query or userinfo would smuggle a credential into a URL we log or forward."""
    with pytest.raises(ValueError, match="HTTPS endpoint without credentials"):
        _https_url(url)


def test_a_trailing_slash_is_stripped_from_an_accepted_endpoint() -> None:
    assert _https_url("https://cloud.gitlab.com/") == "https://cloud.gitlab.com"
    assert _https_url("https://cloud.gitlab.com") == "https://cloud.gitlab.com"


def test_a_non_string_endpoint_is_rejected_not_echoed() -> None:
    """The value can be a service key: it must never reach the message."""
    with pytest.raises(ValueError, match="HTTPS endpoint without credentials") as caught:
        _https_url({"url": "https://secret-in-here"})  # type: ignore[arg-type]

    assert "secret-in-here" not in str(caught.value)


_SAP_OK = {
    "url": "https://tenant.authentication.eu10.hana.ondemand.com",
    "clientid": "client-id",
    "clientsecret": "client-secret-value",
    "serviceurls": {"AI_API_URL": "https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com"},
}


@pytest.mark.parametrize(
    "key",
    [
        "not-json-at-all",
        json.dumps(["https://array.example"]),
        json.dumps({**_SAP_OK, "url": ""}),
        json.dumps({k: v for k, v in _SAP_OK.items() if k != "clientid"}),
        json.dumps({**_SAP_OK, "clientsecret": "   "}),
        json.dumps({**_SAP_OK, "serviceurls": "https://not-a-map"}),
        json.dumps({**_SAP_OK, "serviceurls": {}}),
        json.dumps({**_SAP_OK, "serviceurls": {"AI_API_URL": "http://api.insecure.example"}}),
    ],
)
def test_a_malformed_sap_service_key_is_rejected_without_echoing_it(key: str) -> None:
    """The key holds the client secret; the message must carry none of it."""
    with pytest.raises(ValueError, match="valid AICORE_SERVICE_KEY JSON record") as caught:
        ProviderTokenAuth("sap-ai-core", key, "https://api.example.com")

    assert "client-secret-value" not in str(caught.value)
    assert "not-json-at-all" not in str(caught.value)
    assert "insecure.example" not in str(caught.value)


def test_a_well_formed_sap_service_key_is_accepted() -> None:
    """The rejection tests above must not be passing for the wrong reason."""
    auth = ProviderTokenAuth("sap-ai-core", json.dumps(_SAP_OK), "https://api.example.com")

    assert auth.service["clientid"] == "client-id"
    assert auth.service["serviceurls"]["AI_API_URL"].startswith("https://api.ai.prod")


def test_a_gitlab_model_needs_a_supported_chat_model_override() -> None:
    """An empty override is a workflow model: the request would go to the wrong API."""
    with pytest.raises(ValueError, match="Choose a supported GitLab Duo chat model"):
        build_managed_model("gitlab", "duo-chat", "private-key", {})


def test_building_a_sap_model_rejects_a_malformed_service_key_early() -> None:
    """Validation happens before any client is constructed or env is read."""
    with pytest.raises(ValueError, match="valid AICORE_SERVICE_KEY JSON record"):
        build_managed_model("sap-ai-core", "gpt-4o", "not-json-at-all", {"protocol": "chat"})
