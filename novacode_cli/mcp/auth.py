"""OAuth and secret-backed HTTP headers for remote MCP servers."""

from __future__ import annotations

import asyncio
import hashlib
import webbrowser
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from novacode_cli.onboarding import SecretManager


def store_header(value: str) -> str:
    """Store a header value and return an opaque config reference."""
    if not value or "\r" in value or "\n" in value:
        message = "Invalid MCP header value"
        raise ValueError(message)
    reference = f"mcp-header-{uuid4().hex}"
    if not SecretManager().store_secret(reference, value):
        message = "Could not securely store the MCP header"
        raise RuntimeError(message)
    return reference


def resolve_header(reference: str) -> str:
    """Read a configured header without exposing its value in config or logs."""
    value = SecretManager().get_secret(reference)
    if value is None:
        message = "An MCP header secret is missing; configure it again"
        raise ValueError(message)
    if "\r" in value or "\n" in value:
        message = "Invalid stored MCP header value"
        raise ValueError(message)
    return value


class MCPTokenStorage:
    """Persist SDK token and registration models in Nova's secret store."""

    def __init__(self, server_url: str) -> None:
        """Use a stable secret key for one server URL."""
        digest = hashlib.sha256(server_url.encode()).hexdigest()
        self._tokens_key = f"mcp-oauth-{digest}-tokens"
        self._client_key = f"mcp-oauth-{digest}-client"

    async def get_tokens(self) -> OAuthToken | None:
        """Load stored access and refresh tokens."""
        raw = await asyncio.to_thread(SecretManager().get_secret, self._tokens_key)
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Save access and refresh tokens."""
        saved = await asyncio.to_thread(
            SecretManager().store_secret, self._tokens_key, tokens.model_dump_json()
        )
        if not saved:
            message = "Could not securely store MCP OAuth tokens"
            raise RuntimeError(message)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Load the registered OAuth client."""
        raw = await asyncio.to_thread(SecretManager().get_secret, self._client_key)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Save the registered OAuth client."""
        saved = await asyncio.to_thread(
            SecretManager().store_secret, self._client_key, client_info.model_dump_json()
        )
        if not saved:
            message = "Could not securely store MCP OAuth registration"
            raise RuntimeError(message)


def _loopback_port(server_url: str) -> int:
    # A stable redirect URI lets a stored dynamic registration work next run.
    return 40500 + int(hashlib.sha256(server_url.encode()).hexdigest()[:8], 16) % 10000


def oauth_provider(server_url: str) -> OAuthClientProvider:  # noqa: PLR0915
    """Build an SDK provider with a browser and local callback for sign-in."""
    port = _loopback_port(server_url)
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    callback: asyncio.Future[tuple[str, str | None]] | None = None
    listener: asyncio.AbstractServer | None = None

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal callback
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            target = line.decode("ascii", errors="replace").split(" ", 2)[1]
            path = urlsplit(target)
            values = parse_qs(path.query)
            if path.path != "/callback" or not callback or callback.done():
                response = "Invalid OAuth callback"
            elif values.get("error"):
                callback.set_exception(ValueError("MCP authorization was declined"))
                response = "Authorization declined. Return to Nova."
            elif values.get("code") and values.get("state"):
                callback.set_result((values["code"][0], values["state"][0]))
                response = "Authentication complete. Return to Nova."
            else:
                response = "Invalid OAuth callback"
            body = response.encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def redirect_handler(url: str) -> None:
        nonlocal callback, listener
        callback = asyncio.get_running_loop().create_future()
        listener = await asyncio.start_server(receive, "127.0.0.1", port)
        if not await asyncio.to_thread(webbrowser.open, url):
            listener.close()
            await listener.wait_closed()
            message = "Could not open the browser for MCP authentication"
            raise RuntimeError(message)

    async def callback_handler() -> tuple[str, str | None]:
        nonlocal listener
        try:
            if callback is None:
                message = "MCP OAuth callback was not started"
                raise RuntimeError(message)
            return await asyncio.wait_for(callback, timeout=300)
        finally:
            if listener is not None:
                listener.close()
                await listener.wait_closed()
                listener = None

    metadata = OAuthClientMetadata(
        redirect_uris=[AnyUrl(redirect_uri)],
        client_name="NovaCode",
    )
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=MCPTokenStorage(server_url),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
