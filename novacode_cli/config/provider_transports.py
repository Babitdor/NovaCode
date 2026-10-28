"""Lazy credential exchanges for GitLab Duo, IBM watsonx, and SAP AI Core.

Model construction does not exchange tokens. The SDK's first request obtains
credentials, cached in memory and confined to the configured model origin.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from langchain_core.language_models import BaseChatModel


def _https_url(value: str) -> str:
    """Validate configured credential endpoints without echoing their contents."""
    parts = urlsplit(value if isinstance(value, str) else "")
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        message = "Managed provider authentication requires an HTTPS endpoint without credentials"
        raise ValueError(message)
    return value.rstrip("/")


class ProviderTokenAuth(httpx.Auth):
    """Refresh a provider token on demand, shared by sync and async SDK calls."""

    def __init__(self, provider: str, key: str, endpoint: str) -> None:
        """Keep secrets in memory and bind authorization to one origin."""
        self.provider = provider
        self._key = key
        self._origin = httpx.URL(_https_url(endpoint))
        self._headers: dict[str, str] = {}
        self._expires = 0.0
        self._lock = threading.Lock()
        self.service: dict[str, Any] = {}
        if provider == "sap-ai-core":
            try:
                self.service = json.loads(key)
                if not isinstance(self.service, dict):
                    raise TypeError  # noqa: TRY301 — reject non-object service keys
                for field in ("url", "clientid", "clientsecret"):
                    if (
                        not isinstance(self.service.get(field), str)
                        or not self.service[field].strip()
                    ):
                        raise ValueError  # noqa: TRY301 — reject malformed service-key fields
                if not isinstance(self.service.get("serviceurls"), dict):
                    raise TypeError  # noqa: TRY301 — reject malformed service URL maps
                _https_url(self.service["url"])
                _https_url(self.service["serviceurls"]["AI_API_URL"])
            except (ValueError, KeyError, TypeError):
                message = "SAP AI Core requires a valid AICORE_SERVICE_KEY JSON record"
                raise ValueError(message) from None

    def _load_headers(self) -> dict[str, str]:
        with self._lock:
            if self._expires > time.monotonic():
                return self._headers
            try:
                with httpx.Client(timeout=30, follow_redirects=False) as client:
                    if self.provider == "gitlab":
                        instance = _https_url(
                            os.environ.get("GITLAB_INSTANCE_URL", "https://gitlab.com")
                        )
                        response = client.post(
                            instance + "/api/v4/ai/third_party_agents/direct_access",
                            headers={"Authorization": f"Bearer {self._key}"},
                            json={},
                        )
                    elif self.provider == "watsonx":
                        response = client.post(
                            "https://iam.cloud.ibm.com/identity/token",
                            data={
                                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                                "apikey": self._key,
                            },
                        )
                    else:
                        response = client.post(
                            self.service["url"].rstrip("/") + "/oauth/token",
                            auth=(self.service["clientid"], self.service["clientsecret"]),
                            data={"grant_type": "client_credentials"},
                        )
                    response.raise_for_status()
                    data = response.json()
                token = data["token" if self.provider == "gitlab" else "access_token"]
                if (
                    not isinstance(token, str)
                    or not token.strip()
                    or "\r" in token
                    or "\n" in token
                ):
                    raise ValueError  # noqa: TRY301 — reject malformed token responses
                headers = data.get("headers", {}) if self.provider == "gitlab" else {}
                if not isinstance(headers, dict) or any(
                    not isinstance(k, str)
                    or not isinstance(v, str)
                    or "\r" in k
                    or "\n" in k
                    or "\r" in v
                    or "\n" in v
                    for k, v in headers.items()
                ):
                    raise ValueError  # noqa: TRY301 — reject malformed token metadata
                # Gateway-issued metadata is permitted; it cannot override
                # routing, SDK authentication, framing, or the bearer token.
                self._headers = {
                    k: v for k, v in headers.items() if k.lower().startswith("x-gitlab-")
                }
                self._headers["Authorization"] = f"Bearer {token}"
                lifetime = min(float(data.get("expires_in", 1500)), 1500)
                self._expires = time.monotonic() + max(0, lifetime - 60)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
                # Never include token responses, service keys, or request bodies.
                message = (
                    f"{self.provider} credential exchange failed; check /auth and account access"
                )
                raise RuntimeError(message) from None
            return self._headers

    def _check_origin(self, request: httpx.Request) -> None:
        if (request.url.scheme, request.url.host, request.url.port) != (
            self._origin.scheme,
            self._origin.host,
            self._origin.port,
        ):
            message = "Provider credentials cannot be sent to another origin"
            raise RuntimeError(message)

    def _authorize(self, request: httpx.Request, headers: dict[str, str]) -> None:
        self._check_origin(request)
        request.headers.pop("x-api-key", None)
        request.headers.update(headers)
        if self.provider == "watsonx" and request.url.path.endswith("/chat/completions"):
            streaming = json.loads(request.content).get("stream", False)
            path = request.url.path.removesuffix("/chat/completions")
            request.url = request.url.copy_with(
                path=path + ("/text/chat_stream" if streaming else "/text/chat")
            )

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Authorize synchronous and streaming SDK requests."""
        self._check_origin(request)
        self._authorize(request, self._load_headers())
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Refresh away from the event loop for asynchronous SDK requests."""
        self._check_origin(request)
        self._authorize(request, await asyncio.to_thread(self._load_headers))
        yield request


def build_managed_model(
    provider: str,
    model_name: str,
    key: str,
    override: dict[str, Any],
) -> BaseChatModel:
    """Build a standard SDK client with provider-owned credential transport."""
    from novacode_cli.config.model_create import _opencode_chat_class
    from novacode_cli.config.model_manager import provider_base_url
    from novacode_cli.utils.backend_patches import apply_openai_reasoning_content_patch

    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    body: dict[str, str] = {}
    protocol = override.get("protocol", "chat")
    if provider == "gitlab":
        if not override:
            message = "Choose a supported GitLab Duo chat model; workflow models use a separate API"
            raise ValueError(message)
        endpoint = provider_base_url(provider) + (
            "/anthropic" if protocol == "messages" else "/openai/v1"
        )
        wire_model = override["model"]
    elif provider == "watsonx":
        endpoint = provider_base_url(provider)
        wire_model = model_name
        query = {"version": "2024-05-31"}
        body = {"project_id": os.environ["WATSONX_AI_PROJECT_ID"]}
    else:
        try:
            service = json.loads(key)
            api_url = _https_url(service["serviceurls"]["AI_API_URL"])
        except (ValueError, KeyError, TypeError):
            message = "SAP AI Core requires a valid AICORE_SERVICE_KEY JSON record"
            raise ValueError(message) from None
        deployment = quote(os.environ["SAP_AI_DEPLOYMENT_ID"], safe="")
        endpoint = api_url + "/v2/inference/deployments/" + deployment
        headers = {"AI-Resource-Group": os.environ.get("AICORE_RESOURCE_GROUP", "default")}
        wire_model = model_name
    auth = ProviderTokenAuth(provider, key, endpoint)
    sync = httpx.Client(auth=auth, timeout=300, follow_redirects=False)
    async_ = httpx.AsyncClient(auth=auth, timeout=300, follow_redirects=False)
    if protocol == "messages":
        import anthropic
        from langchain_anthropic import ChatAnthropic
        from pydantic import SecretStr

        model = ChatAnthropic(
            model_name=wire_model,
            api_key=SecretStr("nova-managed"),
            base_url=endpoint,
            max_tokens_to_sample=16_384,
            max_retries=5,
            timeout=300,
            stop=None,
        )
        model.__dict__["_client"] = anthropic.Anthropic(
            api_key="nova-managed",
            base_url=endpoint,
            max_retries=5,
            http_client=sync,
        )
        model.__dict__["_async_client"] = anthropic.AsyncAnthropic(
            api_key="nova-managed",
            base_url=endpoint,
            max_retries=5,
            http_client=async_,
        )
        return model
    apply_openai_reasoning_content_patch()
    return _opencode_chat_class()(
        model=wire_model,
        api_key="nova-managed",
        base_url=endpoint,
        max_retries=5,
        stream_usage=False,
        use_responses_api=protocol == "responses",
        default_headers=headers,
        default_query=query,
        extra_body=body,
        http_client=sync,
        http_async_client=async_,
    )
