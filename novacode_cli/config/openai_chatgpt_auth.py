"""OpenAI Sign in with ChatGPT for local open-source clients.

This module implements OpenAI's ChatGPT-plan token-sharing flow. Tokens are
kept in Nova's OS credential store, never exported to the process environment.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AUTHORIZATION_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/auth/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_CREDENTIAL_NAME = "openai_chatgpt_credentials"
_HOST_ID_NAME = "openai_chatgpt_host_id"
_MIN_REFRESH_SECONDS = 120
_TOKEN_LOCK = threading.RLock()
_TOKEN_RECORD: dict[str, Any] | None = None
_HTTP_CLIENT_LOCK = threading.Lock()
_HTTP_SYNC_CLIENT: httpx.Client | None = None
_HTTP_ASYNC_CLIENT: httpx.AsyncClient | None = None


def _secret_manager():
    from novacode_cli.onboarding import SecretManager

    return SecretManager()


def load_credentials() -> dict[str, Any] | None:
    """Read the saved ChatGPT credential record from the OS credential store."""
    try:
        raw = _secret_manager().get_secret(_CREDENTIAL_NAME)
        data = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — unavailable/corrupt auth is treated as absent
        logger.warning("Could not read the stored OpenAI ChatGPT sign-in")
        return None
    if not isinstance(data, dict):
        return None
    required_text = ("client_id", "subject", "id_token", "access_token", "refresh_token")
    if any(not isinstance(data.get(key), str) or not data[key] for key in required_text):
        return None
    scopes = data.get("scopes")
    if (
        not isinstance(scopes, list)
        or "chatgpt.tokens.use.direct" not in scopes
        or any(not isinstance(scope, str) for scope in scopes)
    ):
        return None
    try:
        expires_at = float(data["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return data if math.isfinite(expires_at) else None


def has_credentials() -> bool:
    """Whether a ChatGPT-plan credential is stored."""
    return load_credentials() is not None


def selected_auth_mode() -> str:
    """Return the OpenAI credential source selected in `/auth`."""
    try:
        from novacode_cli.config.nova_config import NovaConfig

        return NovaConfig().get_openai_auth_mode()
    except Exception:  # noqa: BLE001 — an old/corrupt config uses auto selection
        return "auto"


def set_auth_mode(mode: str) -> None:
    """Persist the selected OpenAI credential source."""
    from novacode_cli.config.nova_config import NovaConfig

    NovaConfig().set_openai_auth_mode(mode)


def delete_credentials() -> bool:
    """Remove the ChatGPT tokens while retaining this host's stable ID."""
    global _TOKEN_RECORD
    try:
        removed = bool(_secret_manager().delete_secret(_CREDENTIAL_NAME))
        with _TOKEN_LOCK:
            _TOKEN_RECORD = None
        return removed
    except Exception:  # noqa: BLE001 — report a safe failure to the UI
        logger.warning("Could not remove the stored OpenAI ChatGPT sign-in")
        return False


def _save_credentials(record: dict[str, Any]) -> None:
    global _TOKEN_RECORD
    if not _secret_manager().store_secret(_CREDENTIAL_NAME, json.dumps(record)):
        raise RuntimeError("Nova could not save the OpenAI sign-in to the OS credential store.")
    with _TOKEN_LOCK:
        _TOKEN_RECORD = dict(record)


def _host_id() -> str:
    manager = _secret_manager()
    existing = manager.get_secret(_HOST_ID_NAME)
    if existing:
        return existing
    value = f"urn:uuid:{uuid.uuid4()}"
    if not manager.store_secret(_HOST_ID_NAME, value):
        raise RuntimeError("Nova could not save its OpenAI sign-in host ID.")
    return value


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _verify_id_token(id_token: str, *, client_id: str, nonce: str) -> dict[str, Any]:
    """Verify signature and OIDC claims against OpenAI's published JWKS."""
    try:
        import jwt

        key = jwt.PyJWKClient(JWKS_URL, timeout=10).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=client_id,
            issuer=ISSUER,
            leeway=5,
            options={"require": ["sub", "exp", "iat", "nonce"]},
        )
    except Exception as exc:
        raise RuntimeError("OpenAI sign-in returned an invalid identity token.") from exc
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise RuntimeError("OpenAI sign-in identity check failed. Please try again.")
    return claims


class _CallbackHandler(BaseHTTPRequestHandler):
    """Capture one local OAuth callback without exposing it to the LAN."""

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if urllib.parse.urlsplit(self.path).path != CALLBACK_PATH:
            self.send_error(404)
            return
        self.server.callback_query = urllib.parse.parse_qs(  # type: ignore[attr-defined]
            urllib.parse.urlsplit(self.path).query, keep_blank_values=True
        )
        body = b"OpenAI sign-in received. You can close this browser tab and return to Nova."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def sign_in(timeout: float = 300) -> dict[str, Any]:
    """Run OpenAI's loopback Authorization Code + PKCE flow and store tokens.

    The listener binds only to 127.0.0.1, accepts the fixed callback path, and
    validates state before exchanging the one-time authorization code.
    """
    old = load_credentials()
    host_id = _host_id()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())

    server = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.daemon_threads = True
    server.timeout = 1
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
    authorization_client_id = old.get("client_id") if old else "dynamic_agent_client"
    params = {
        "client_id": authorization_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "ext_agent_host_id": host_id,
    }
    if old:
        params["id_token_hint"] = str(old.get("id_token", ""))
        params.pop("agent_name_hint", None)
    else:
        params["agent_name_hint"] = "NovaCode"
    url = f"{AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(params)}"
    completed = threading.Event()
    stop_listener = threading.Event()
    failures: list[BaseException] = []

    def listen() -> None:
        deadline = time.monotonic() + timeout
        try:
            while (
                time.monotonic() < deadline
                and not stop_listener.is_set()
                and not getattr(server, "callback_query", None)
            ):
                server.handle_request()
            if not getattr(server, "callback_query", None):
                raise TimeoutError("OpenAI sign-in timed out. Run /auth and try again.")
        except BaseException as exc:  # propagate to the calling worker
            failures.append(exc)
        finally:
            completed.set()

    listener = threading.Thread(target=listen, name="nova-openai-oauth", daemon=True)
    listener.start()
    try:
        if not webbrowser.open(url):
            raise RuntimeError(
                "Nova could not open your browser. OpenAI sign-in requires a browser callback."
            )
        if not completed.wait(timeout + 2):
            raise TimeoutError("OpenAI sign-in timed out. Run /auth and try again.")
        if failures:
            raise failures[0]
        query = getattr(server, "callback_query", {})
        if not hmac.compare_digest((query.get("state") or [""])[0], state):
            raise RuntimeError("OpenAI sign-in state did not match. Please try again.")
        if (query.get("error") or [""])[0]:
            raise RuntimeError("OpenAI sign-in was cancelled or denied.")
        code = (query.get("code") or [""])[0]
        client_id = (query.get("client_id") or [authorization_client_id])[0]
        if not code or not client_id or client_id == "dynamic_agent_client":
            raise RuntimeError("OpenAI did not complete Nova's client registration.")
        if old and client_id != old.get("client_id"):
            raise RuntimeError(
                "OpenAI returned a different registered client. Existing sign-in kept."
            )

        response = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "resource": RESOURCE,
            },
            timeout=20,
        )
        response.raise_for_status()
        tokens = response.json()
        if not isinstance(tokens, dict):
            raise RuntimeError("OpenAI returned an invalid token response.")
        granted = set(str(tokens.get("scope") or (query.get("scope") or [""])[0]).split())
        if "chatgpt.tokens.use.direct" not in granted:
            raise RuntimeError(
                "This OpenAI account did not grant ChatGPT plan usage. "
                "Enable it in OpenAI and sign in again."
            )
        id_token = tokens.get("id_token")
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not all(
            isinstance(value, str) and value for value in (id_token, access_token, refresh_token)
        ):
            raise RuntimeError(
                "OpenAI did not return all credentials required for a durable sign-in."
            )
        identity = _verify_id_token(id_token, client_id=client_id, nonce=nonce)
        if old and identity.get("sub") != old.get("subject"):
            raise RuntimeError("The selected OpenAI account changed. Existing sign-in kept.")
        record = {
            "client_id": client_id,
            "host_id": host_id,
            "subject": identity["sub"],
            "email": identity.get("email"),
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "scopes": sorted(granted),
            "expires_at": time.time() + max(60, int(tokens.get("expires_in", 3600))),
            "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _save_credentials(record)
        return record
    finally:
        stop_listener.set()
        server.server_close()
        listener.join(timeout=2)


def access_token() -> str:
    """Return a current access token, refreshing and rotating credentials as needed."""
    with _TOKEN_LOCK:
        return _access_token_locked()


def _access_token_locked() -> str:
    """Resolve or refresh a token while the process-wide refresh lock is held."""
    global _TOKEN_RECORD
    if _TOKEN_RECORD is None:
        _TOKEN_RECORD = load_credentials()
    record = _TOKEN_RECORD
    if not record:
        raise RuntimeError("Sign in to ChatGPT in /auth before selecting this OpenAI account.")
    if float(record.get("expires_at", 0)) > time.time() + _MIN_REFRESH_SECONDS:
        return str(record["access_token"])
    try:
        response = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "client_id": record["client_id"],
                "refresh_token": record["refresh_token"],
                "resource": RESOURCE,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise RuntimeError("OpenAI sign-in could not refresh. Reconnect with /auth.") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("OpenAI token refresh failed. Sign in again with /auth.")
    scopes = set(str(data.get("scope") or " ".join(record.get("scopes", []))).split())
    if "chatgpt.tokens.use.direct" not in scopes:
        raise RuntimeError(
            "OpenAI sign-in no longer allows ChatGPT plan usage. Reconnect with /auth."
        )
    record["access_token"] = data["access_token"]
    record["refresh_token"] = data.get("refresh_token") or record["refresh_token"]
    record["expires_at"] = time.time() + max(60, int(data.get("expires_in", 3600)))
    record["scopes"] = sorted(scopes)
    _save_credentials(record)
    return str(record["access_token"])


class ChatGPTPlanAuth(httpx.Auth):
    """Inject a freshly refreshed bearer token into OpenAI SDK requests."""

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {access_token()}"
        yield request

    async def async_auth_flow(self, request: httpx.Request):
        token = await asyncio.to_thread(access_token)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def http_clients() -> tuple[httpx.Client, httpx.AsyncClient]:
    """Return process-shared SDK transports so model rebuilds do not leak pools."""
    global _HTTP_SYNC_CLIENT, _HTTP_ASYNC_CLIENT
    with _HTTP_CLIENT_LOCK:
        if _HTTP_SYNC_CLIENT is None:
            _HTTP_SYNC_CLIENT = httpx.Client(auth=ChatGPTPlanAuth())
        if _HTTP_ASYNC_CLIENT is None:
            _HTTP_ASYNC_CLIENT = httpx.AsyncClient(auth=ChatGPTPlanAuth())
        return _HTTP_SYNC_CLIENT, _HTTP_ASYNC_CLIENT
