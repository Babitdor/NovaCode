"""Google Desktop OAuth for Gemini's bearer-authenticated REST API.

Tokens never become API-key environment variables. The SDK's HTTP clients use
this auth adapter for both ordinary and streaming requests.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import socket
import threading
import time
import webbrowser
from collections.abc import AsyncIterator, Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import requests

if TYPE_CHECKING:
    from novacode_cli.onboarding import SecretManager

SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/generative-language.retriever",
)
CREDENTIAL_NAME = "google_oauth_credentials"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
_LOCK = threading.RLock()


def _manager() -> SecretManager:
    from novacode_cli.onboarding import SecretManager

    return SecretManager()


def read_client_config(path: Path) -> dict:
    """Accept only bounded Google Desktop client files using official servers."""
    try:
        with Path(path).expanduser().open("rb") as stream:
            raw = stream.read(65537)
        data = json.loads(raw) if len(raw) <= 65536 else None
        client = data.get("installed") if isinstance(data, dict) else None
        if not isinstance(client, dict) or (
            client.get("auth_uri") not in AUTH_URIS
            or client.get("token_uri") != TOKEN_URI
            or not isinstance(client.get("client_id"), str)
            or not client["client_id"].endswith(".apps.googleusercontent.com")
            or not isinstance(client.get("client_secret"), str)
            or not client["client_secret"]
        ):
            raise ValueError
        return {"installed": client}
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Choose a valid Google Desktop OAuth client JSON file.") from exc


def _valid_record(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and data.get("token_uri") == TOKEN_URI
        and all(
            isinstance(data.get(key), str) and data[key]
            for key in ("token", "refresh_token", "client_id", "client_secret")
        )
        and isinstance(data.get("scopes"), list)
        and all(isinstance(scope, str) for scope in data["scopes"])
        and set(SCOPES).issubset(data["scopes"])
        and isinstance(data.get("quota_project_id"), str)
        and bool(re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", data["quota_project_id"]))
    )


def load_credentials() -> dict | None:
    """Read a usable refresh record without exposing credential details."""
    try:
        raw = _manager().get_secret(CREDENTIAL_NAME)
        data = json.loads(raw) if raw else None
        if not _valid_record(data):
            return None
        from google.oauth2.credentials import Credentials

        Credentials.from_authorized_user_info(data)
        return data
    except Exception:  # noqa: BLE001 — malformed/unavailable secrets are not connected
        return None


def has_credentials() -> bool:
    return load_credentials() is not None


def save_credentials(record: dict) -> None:
    """Keep all OAuth secrets in Nova's existing credential backend."""
    if not _valid_record(record):
        raise RuntimeError("Google sign-in did not return usable refresh credentials.")
    with _LOCK:
        if not _manager().store_secret(CREDENTIAL_NAME, json.dumps(record)):
            raise RuntimeError("Nova could not save the Google sign-in credentials.")


def delete_credentials() -> bool:
    with _LOCK:
        try:
            return bool(_manager().delete_secret(CREDENTIAL_NAME))
        except Exception:  # noqa: BLE001 — report failure without credential details
            return False


def selected_auth_mode() -> str:
    from novacode_cli.config.nova_config import NovaConfig

    config = NovaConfig()
    if hasattr(config, "_load"):
        config._load()
    mode = config.get("google_auth_mode", "auto")
    return mode if mode in {"auto", "api_key", "oauth"} else "auto"


def set_auth_mode(mode: str) -> None:
    from novacode_cli.config.nova_config import NovaConfig

    if mode not in {"auto", "api_key", "oauth"}:
        raise ValueError("Google auth mode must be auto, api_key, or oauth.")
    NovaConfig().set("google_auth_mode", mode)


def use_oauth(*, has_key: bool) -> bool:
    mode = selected_auth_mode()
    return mode == "oauth" or (mode == "auto" and not has_key and has_credentials())


def sign_in(
    path: Path,
    project: str,
    cancel: threading.Event,
    *,
    timeout: float = 180,
    on_url: Callable[[str], None] | None = None,
) -> dict:
    """Run cancellable loopback PKCE sign-in; the caller explicitly commits it."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("Enter your Google Cloud project ID (not its display name).")
    flow = InstalledAppFlow.from_client_config(
        read_client_config(path), SCOPES, autogenerate_code_verifier=True
    )
    outcome: dict[str, str] = {}
    state = ""

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # callback URLs include authorization codes

        def do_GET(self) -> None:  # noqa: N802 — HTTP handler API
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            valid = (
                len(self.path) <= 8192
                and parts.path == "/oauth/callback"
                and len(query.get("state", [])) == 1
                and query["state"][0].isascii()
                and hmac.compare_digest(query["state"][0], state)
            )
            if valid and len(query.get("code", [])) == 1 and not query.get("error"):
                outcome["code"] = query["code"][0]
            elif valid and query.get("error"):
                outcome["error"] = "Google sign-in was declined."
            else:
                valid = False
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Return to Nova to finish sign-in." if valid else b"Invalid callback."
            )

    class LoopbackServer(HTTPServer):
        def get_request(self) -> tuple[socket.socket, tuple]:
            connection, address = super().get_request()
            connection.settimeout(2)
            return connection, address

    try:
        with LoopbackServer(("127.0.0.1", 0), Callback) as server:
            server.timeout = 0.2
            flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth/callback"
            url, state = flow.authorization_url(access_type="offline", prompt="consent")
            if cancel.is_set():
                raise RuntimeError("Google sign-in was cancelled.")
            if on_url:
                on_url(url)
            webbrowser.open(url)
            deadline = time.monotonic() + timeout
            while not outcome and not cancel.is_set() and time.monotonic() < deadline:
                server.handle_request()
            if cancel.is_set():
                raise RuntimeError("Google sign-in was cancelled.")
            if "error" in outcome:
                raise RuntimeError(outcome["error"])
            if "code" not in outcome:
                raise RuntimeError("Google sign-in timed out. Try again.")
            flow.fetch_token(code=outcome["code"], timeout=30)
            record = json.loads(flow.credentials.to_json())
            record["quota_project_id"] = project
            if cancel.is_set():
                raise RuntimeError("Google sign-in was cancelled.")
            if not _valid_record(record):
                raise RuntimeError("Google sign-in did not grant the required API access.")
            return record
    except (OSError, httpx.HTTPError) as exc:
        raise RuntimeError(
            "Google sign-in could not connect. Check your network and try again."
        ) from exc
    finally:
        flow.oauth2session.close()


def _bearer() -> tuple[str, str]:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    with _LOCK:
        record = load_credentials()
        if record is None:
            raise RuntimeError("Google sign-in is missing. Reconnect through /auth.")
        credentials = Credentials.from_authorized_user_info(record)
        if not credentials.valid:
            try:
                with requests.Session() as session:
                    transport = Request(session=session)
                    credentials.refresh(lambda *a, **kw: transport(*a, **{**kw, "timeout": 30}))
                refreshed = json.loads(credentials.to_json())
                refreshed["quota_project_id"] = record["quota_project_id"]
                save_credentials(refreshed)
            except Exception as exc:  # noqa: BLE001 — never display token response bodies
                raise RuntimeError(
                    "Google sign-in expired or refresh failed. Reconnect through /auth."
                ) from exc
        return credentials.token, record["quota_project_id"]


def access_token() -> str:
    return _bearer()[0]


class GoogleOAuthAuth(httpx.Auth):
    """Inject bearer auth without changing the SDK's Gemini request format."""

    @staticmethod
    def _check_endpoint(request: httpx.Request) -> None:
        if request.url.scheme != "https" or request.url.host != "generativelanguage.googleapis.com":
            raise RuntimeError("Google OAuth requires the official Gemini API endpoint.")

    @staticmethod
    def _apply(request: httpx.Request, token: str, project: str) -> None:
        request.headers.pop("x-goog-api-key", None)
        request.headers["Authorization"] = f"Bearer {token}"
        request.headers["x-goog-user-project"] = project

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        self._check_endpoint(request)
        self._apply(request, *_bearer())
        yield request

    async def async_auth_flow(self, request: httpx.Request) -> AsyncIterator[httpx.Request]:
        self._check_endpoint(request)
        self._apply(request, *(await asyncio.to_thread(_bearer)))
        yield request
