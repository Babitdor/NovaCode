"""Offline MCP OAuth, secret-header, CLI, and TUI checks."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from textual.app import App
from textual.widgets import Button, OptionList

from novacode_cli.cli_args import setup_mcp_parser
from novacode_cli.mcp import auth, commands
from novacode_cli.mcp.client import build_mcp_server_config
from novacode_cli.mcp.config import MCPServerConfig
from novacode_cli.tui.screens import McpScreen

if TYPE_CHECKING:
    from pathlib import Path



class _Secrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def store_secret(self, key: str, value: str) -> bool:
        self.values[key] = value
        return True

    def get_secret(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.mark.asyncio
async def test_oauth_tokens_and_registration_use_secret_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = _Secrets()
    monkeypatch.setattr(auth, "SecretManager", lambda: secrets)
    storage = auth.MCPTokenStorage("https://example.test/mcp")
    tokens = OAuthToken(
        access_token="private-access", token_type="Bearer", refresh_token="private-refresh"  # noqa: S106
    )
    client = OAuthClientInformationFull(
        client_id="nova-test", redirect_uris=["http://127.0.0.1:40500/callback"]
    )
    await storage.set_tokens(tokens)
    await storage.set_client_info(client)
    restored = await auth.MCPTokenStorage("https://example.test/mcp").get_tokens()
    assert restored is not None
    assert restored.refresh_token == tokens.refresh_token
    assert (await storage.get_client_info()).client_id == "nova-test"
    assert "private-access" not in repr(storage)


def test_cli_parses_positional_url_and_repeatable_headers() -> None:
    parser = argparse.ArgumentParser()
    setup_mcp_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args([
        "mcp", "add", "--transport", "http", "private-api",
        "https://api.example.test/mcp", "--header", "X-API-Key: secret-value",
    ])
    assert args.endpoint == "https://api.example.test/mcp"
    assert args.header == ["X-API-Key: secret-value"]


def test_http_connection_resolves_secret_header_and_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _Secrets()
    monkeypatch.setattr(auth, "SecretManager", lambda: secrets)
    reference = auth.store_header("private-value")
    config = MCPServerConfig(
        transport="http", url="https://example.test/mcp",
        header_secrets={"X-API-Key": reference}, oauth=True,
    )
    assert "private-value" not in config.model_dump_json()
    connection = build_mcp_server_config(config)
    assert connection["headers"]["X-API-Key"] == "private-value"
    assert isinstance(connection["auth"], auth.OAuthClientProvider)
    assert auth._loopback_port(config.url) == auth._loopback_port(config.url)


def test_header_validation_rejects_smuggling_inputs() -> None:
    with pytest.raises(ValueError, match="Invalid MCP header value"):
        auth.store_header("secret\r\nAnother: value")
    with pytest.raises(ValueError, match="Protected HTTP header"):
        MCPServerConfig(
            transport="http", url="https://example.test/mcp",
            header_secrets={"Host": "mcp-header-" + "0" * 32},
        )


def test_cli_add_persists_only_header_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.mcp import config as config_module

    secrets = _Secrets()
    monkeypatch.setattr(auth, "SecretManager", lambda: secrets)
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(commands, "MCPConfig", lambda: config_module.MCPConfig(path))
    commands._add(
        "private-api", "http", url="https://api.example.test/mcp",
        headers=["X-API-Key: private-value"],
    )
    assert "private-value" not in path.read_text(encoding="utf-8")
    config = config_module.MCPConfig(path).get_server("private-api")
    assert config is not None
    assert build_mcp_server_config(config)["headers"]["X-API-Key"] == "private-value"


@pytest.mark.asyncio
async def test_browser_callback_returns_code_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.webbrowser, "open", lambda _url: True)
    provider = auth.oauth_provider("https://example.test/mcp")
    redirect_handler = provider.context.redirect_handler
    callback_handler = provider.context.callback_handler
    assert redirect_handler is not None
    assert callback_handler is not None
    url = (
        "https://login.example.test/authorize?redirect_uri="
        + str(provider.context.client_metadata.redirect_uris[0])
    )
    # The callback is delivered through the actual loopback HTTP listener.
    await redirect_handler(url)
    redirect = provider.context.client_metadata.redirect_uris[0]
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{redirect}?code=abc&state=state-1")
    assert response.status_code == 200
    assert await callback_handler() == ("abc", "state-1")


@pytest.mark.asyncio
async def test_mcp_menu_offers_authentication_for_http_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.mcp import client as client_module
    from novacode_cli.mcp import config as config_module

    servers = {"remote": MCPServerConfig(transport="http", url="https://example.test/mcp")}

    class Config:
        def list_servers(self) -> dict[str, MCPServerConfig]:
            return servers

        def get_server(self, name: str) -> MCPServerConfig:
            return servers[name]

        async def add_server_async(self, name: str, config: MCPServerConfig) -> None:
            servers[name] = config

    async def connected(name: str, config: MCPServerConfig) -> tuple[bool, str]:
        assert name == "remote"
        assert config.oauth
        return True, "Connected"

    monkeypatch.setattr(config_module, "MCPConfig", Config)
    monkeypatch.setattr(client_module, "check_server_connection", connected)
    monkeypatch.setattr(
        "novacode_cli.mcp.get_shared_mcp_middleware",
        lambda: SimpleNamespace(_tools_discovered=True, _tools_cache=[]),
    )

    class ScreenApp(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(McpScreen())

    async with ScreenApp().run_test() as pilot:
        screen = pilot.app.screen
        assert isinstance(screen, McpScreen)
        options = screen.query_one("#mcp-configured", OptionList)
        options.highlighted = 0
        await pilot.pause()
        assert screen.query_one("#authenticate", Button).disabled is False
        screen.query_one("#authenticate", Button).press()
        await pilot.pause()
        assert servers["remote"].oauth is True


@pytest.mark.asyncio
async def test_connection_check_reports_the_cause_inside_task_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.client.auth.exceptions import OAuthRegistrationError

    from novacode_cli.mcp import client as client_module

    class Client:
        def __init__(self, servers: object) -> None:
            pass

        async def get_tools(self) -> list:
            refused = OAuthRegistrationError("Registration failed: 403 Forbidden")
            raise ExceptionGroup("unhandled errors in a TaskGroup", [refused])

    async def reachable(name: str, config: MCPServerConfig) -> None:
        return None

    monkeypatch.setattr(client_module, "MultiServerMCPClient", Client)
    monkeypatch.setattr(client_module, "_check_remote_server", reachable)
    monkeypatch.setattr(client_module, "build_mcp_server_config", lambda config: {})
    ok, detail = await client_module.check_server_connection(
        "figma", MCPServerConfig(transport="http", url="https://mcp.figma.com/mcp", oauth=True)
    )
    assert not ok
    assert "Registration failed: 403" in detail


@pytest.mark.asyncio
async def test_figma_refusal_explains_the_allowlist_and_desktop_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual.widgets import Static

    from novacode_cli.mcp import client as client_module
    from novacode_cli.mcp import config as config_module

    servers = {"figma": MCPServerConfig(transport="http", url="https://mcp.figma.com/mcp")}

    class Config:
        def list_servers(self) -> dict[str, MCPServerConfig]:
            return servers

        def get_server(self, name: str) -> MCPServerConfig:
            return servers[name]

    async def refused(name: str, config: MCPServerConfig) -> tuple[bool, str]:
        return False, "Connection failed: Registration failed: 403 Forbidden"

    monkeypatch.setattr(config_module, "MCPConfig", Config)
    monkeypatch.setattr(client_module, "check_server_connection", refused)

    class ScreenApp(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(McpScreen())

    async with ScreenApp().run_test() as pilot:
        screen = pilot.app.screen
        screen.query_one("#mcp-configured", OptionList).highlighted = 0
        await pilot.pause()
        screen.query_one("#authenticate", Button).press()
        await pilot.pause()
        await pilot.pause()
        hint = str(screen.query_one("#mcp-hint", Static).render())
        assert "approved apps" in hint
        assert "figma-desktop" in hint


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (100, 30), (90, 30)])
async def test_every_mcp_button_fits_inside_the_dialog(size: tuple[int, int]) -> None:
    class ScreenApp(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(McpScreen())

    async with ScreenApp().run_test(size=size) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        box = screen.query_one("#modal-box").region
        for button in screen.query("#modal-buttons Button").results(Button):
            assert box.contains_region(button.region), button.id
            assert button.region.width >= len(str(button.label)) + 2, button.id
