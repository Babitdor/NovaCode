"""MCP client for connecting to and managing MCP servers.

This module provides comprehensive functionality to connect to MCP servers using
the langchain-mcp-adapters library, with support for:
- Multiple transport mechanisms (stdio, SSE, HTTP)
- Persistent session management for stateful servers
- Auto-discovery of configuration files
- Security filtering for project-level stdio servers
- Pre-flight validation (command and URL checks)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from langchain_mcp_adapters.client import Connection, MultiServerMCPClient

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import (
    SSEConnection,
    StdioConnection,
    StreamableHttpConnection,
)

from novacode_cli.mcp.config import MCPConfig, MCPServerConfig

logger = logging.getLogger(__name__)


# Supported transport types
_SUPPORTED_REMOTE_TYPES = {"sse", "http"}
"""Supported transport types for remote MCP servers (SSE and HTTP)."""


@dataclass
class MCPToolInfo:
    """Metadata for a single MCP tool."""

    name: str
    """Tool name (may include server name prefix)."""

    description: str
    """Human-readable description of what the tool does."""


@dataclass
class MCPServerInfo:
    """Metadata for a connected MCP server and its tools."""

    name: str
    """Server name from the MCP configuration."""

    transport: str
    """Transport type (`stdio`, `sse`, or `http`)."""

    tools: list[MCPToolInfo] = field(default_factory=list)
    """Tools exposed by this server."""


class MCPSessionManager:
    """Manages persistent MCP sessions for stateful stdio servers.

    This manager creates and maintains persistent sessions for stdio MCP
    servers, preventing server restarts on every tool call. Sessions are kept
    alive until explicitly cleaned up.
    """

    def __init__(self) -> None:
        """Initialize the session manager."""
        self.client: MultiServerMCPClient | None = None
        self.exit_stack = AsyncExitStack()

    async def cleanup(self) -> None:
        """Clean up all managed sessions and close connections."""
        await self.exit_stack.aclose()


def build_mcp_server_config(
    config: MCPServerConfig,
) -> Connection:
    """Convert MCPServerConfig to langchain-mcp-adapters connection format.

    Supports multiple transport types:
    - stdio: Process-based servers with command, args, env
    - sse: Server-Sent Events servers with URL
    - http: HTTP-based servers with URL (uses StreamableHttpConnection)

    Args:
        config: Server configuration

    Returns:
        Connection configuration for MultiServerMCPClient

    Raises:
        ValueError: If transport type is unsupported
    """
    if config.transport == "stdio":
        connection: Connection = StdioConnection(
            transport="stdio",
            command=config.command or "",
            args=config.args or [],
            env=config.env if config.env else None,
        )
    elif config.transport == "http":
        # Use StreamableHttpConnection for HTTP transport
        # Protected headers that must not be overridden via config
        _protected_headers = {"content-length", "transfer-encoding", "host", "connection"}
        headers: dict[str, Any] | None = None
        if config.env:
            headers = {}
            for key, value in config.env.items():
                if key.startswith("HTTP_HEADER_"):
                    header_name = key[12:].replace("_", "-")
                    # Skip protected headers to prevent request smuggling
                    if header_name.lower() in _protected_headers:
                        continue
                    # Skip header values with newlines to prevent header injection
                    if "\n" in value or "\r" in value:
                        continue
                    headers[header_name] = value
            if not headers:
                headers = None

        connection = StreamableHttpConnection(
            transport="streamable_http",
            url=config.url or "",
            headers=headers,
        )
    elif config.transport == "sse":
        # SSE transport
        _protected_headers = {"content-length", "transfer-encoding", "host", "connection"}
        headers: dict[str, Any] | None = None
        if config.env:
            headers = {}
            for key, value in config.env.items():
                if key.startswith("HTTP_HEADER_"):
                    header_name = key[12:].replace("_", "-")
                    if header_name.lower() in _protected_headers:
                        continue
                    if "\n" in value or "\r" in value:
                        continue
                    headers[header_name] = value
            if not headers:
                headers = None

        connection = SSEConnection(
            transport="sse",
            url=config.url or "",
            headers=headers,
        )
    else:
        msg = f"Unsupported transport type: {config.transport}"
        raise ValueError(msg)

    return connection


def build_mcp_config_dict(mcp_config: MCPConfig) -> dict[str, Connection]:
    """Build configuration dictionary for MultiServerMCPClient.

    Args:
        mcp_config: MCP configuration manager

    Returns:
        Configuration dict for MultiServerMCPClient
    """
    servers = mcp_config.list_servers()
    config_dict: dict[str, Connection] = {}

    for name, config in servers.items():
        if getattr(config, "disabled", False):
            logger.info("Skipping disabled MCP server: %s", name)
            continue
        try:
            config_dict[name] = build_mcp_server_config(config)
        except ValueError:
            # Skip servers with invalid configuration
            logger.warning("Skipping invalid server config: %s", name)
            continue

    return config_dict


def create_mcp_client(
    mcp_config: MCPConfig | None = None,
) -> MultiServerMCPClient:
    """Create a MultiServerMCPClient from MCP configuration.

    Args:
        mcp_config: MCP configuration manager. If None, loads from default path.

    Returns:
        Configured MultiServerMCPClient instance
    """
    if mcp_config is None:
        mcp_config = MCPConfig()

    config_dict = build_mcp_config_dict(mcp_config)

    if not config_dict:
        # Return empty client if no servers configured
        return MultiServerMCPClient(None)

    return MultiServerMCPClient(config_dict)


def _check_stdio_server(server_name: str, config: MCPServerConfig) -> None:
    """Verify that a stdio server's command exists on PATH.

    Args:
        server_name: Name of the server (for error messages).
        config: Server configuration dictionary with `command` key.

    Raises:
        RuntimeError: If the command is missing from config or not found on PATH.
    """
    if not config.command:
        msg = f"MCP server '{server_name}': missing 'command' in config."
        raise RuntimeError(msg)
    if shutil.which(config.command) is None:
        msg = (
            f"MCP server '{server_name}': command '{config.command}' not found on PATH. "
            "Install it or check your MCP config."
        )
        raise RuntimeError(msg)


async def _check_remote_server(server_name: str, config: MCPServerConfig) -> None:
    """Check network connectivity to a remote MCP server URL.

    Sends a lightweight HEAD request with a 2-second timeout to detect DNS
    failures, refused connections, and network timeouts early, before the MCP
    session handshake. HTTP error responses (4xx, 5xx) are not treated as
    failures — only transport errors, invalid URLs, and OS-level socket
    errors raise.

    Args:
        server_name: Name of the server (for error messages).
        config: Server configuration dictionary with `url` key.

    Raises:
        RuntimeError: If the server URL is unreachable or invalid.
    """
    import httpx

    if not config.url:
        msg = f"MCP server '{server_name}': missing 'url' in config."
        raise RuntimeError(msg)
    try:
        async with httpx.AsyncClient() as client:
            await client.head(config.url, timeout=2)
    except (httpx.TransportError, httpx.InvalidURL, OSError) as exc:
        msg = (
            f"MCP server '{server_name}': URL '{config.url}' is unreachable: {exc}. "
            "Check that the URL is correct and the server is running."
        )
        raise RuntimeError(msg) from exc


async def check_server_connection(name: str, config: MCPServerConfig) -> tuple[bool, str]:
    """Check connection to an MCP server with pre-flight validation.

    This function performs health checks before attempting to load tools:
    - For stdio servers: Verifies command exists on PATH
    - For remote servers: Checks URL reachability with HEAD request

    Args:
        name: Server name/identifier
        config: Server configuration

    Returns:
        Tuple of (success, message)
    """
    try:
        # Pre-flight validation
        if config.transport == "stdio":
            _check_stdio_server(name, config)
        elif config.transport in _SUPPORTED_REMOTE_TYPES:
            await _check_remote_server(name, config)

        # Try to load tools
        server_config = build_mcp_server_config(config)
        client = MultiServerMCPClient({name: server_config})

        tools = await client.get_tools()
        return True, f"Connected successfully. Found {len(tools)} tools."

    except Exception as e:
        return False, f"Connection failed: {e}"


def discover_mcp_configs() -> list[Path]:
    """Find MCP config files from standard locations.

    Checks three paths in precedence order (lowest to highest):
    1. ~/.nova/mcp.json (user-level global)
    2. <project-root>/.nova/mcp.json (project subdir)
    3. <project-root>/mcp.json (project root)

    Returns:
        List of existing config file paths, ordered lowest-to-highest precedence.
    """
    from novacode_cli.config.config import HOME_DIR, settings

    user_dir = Path(HOME_DIR)
    # The same project root the rest of Nova uses. This used to run its own
    # walk over ``cwd.parents`` — which skips cwd itself, so launched from the
    # repo root it inspected the folders ABOVE the repo — and accepted
    # pyproject.toml as a marker where everything else wants .git. A parent
    # folder holding either would have claimed the project, and the repo's own
    # .nova/mcp.json would have been silently ignored.
    project_root = settings.get_workspace_root()

    candidates = [
        user_dir / "mcp.json",
        project_root / ".nova" / "mcp.json",
        project_root / "mcp.json",
    ]

    found: list[Path] = []
    for path in candidates:
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            logger.warning("Could not check MCP config %s", path, exc_info=True)
    return found


def load_mcp_config_lenient(config_path: Path) -> dict[str, Any] | None:
    """Load an MCP config file, returning None on any error.

    Wraps config loading with lenient error handling suitable for
    auto-discovery. Missing files are skipped silently; parse and validation
    errors are logged as warnings.

    Args:
        config_path: Path to the MCP config file.

    Returns:
        Parsed config dict, or None if the file is missing or invalid.
    """
    try:
        with config_path.open(encoding="utf-8") as f:
            config = json.load(f)

        # Validate basic structure
        if "mcpServers" not in config:
            logger.warning("MCP config %s missing 'mcpServers' field", config_path)
            return None

        if not isinstance(config["mcpServers"], dict):
            logger.warning("MCP config %s has invalid 'mcpServers' field", config_path)
            return None

        return config
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Skipping unreadable MCP config %s: %s", config_path, e)
        return None
    except json.JSONDecodeError as e:
        logger.warning("Skipping invalid MCP config %s: %s", config_path, e)
        return None


def merge_mcp_configs(configs: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge multiple MCP config dicts by server name.

    Later entries override earlier ones for the same server name
    (simple `dict.update` on `mcpServers`).

    Args:
        configs: Ordered list of parsed config dicts (each with `mcpServers` key).

    Returns:
        Merged config with combined `mcpServers`.
    """
    merged: dict[str, Any] = {}
    for cfg in configs:
        servers = cfg.get("mcpServers")
        if isinstance(servers, dict):
            merged.update(servers)
    return {"mcpServers": merged}


async def load_mcp_tools_with_session(
    mcp_config: MCPConfig | None = None,
) -> tuple[list[BaseTool], MCPSessionManager, list[MCPServerInfo]]:
    """Load MCP tools with persistent session management.

    This creates persistent sessions for stdio servers that remain active
    across tool calls, avoiding server restarts. Sessions are managed by
    MCPSessionManager and should be cleaned up with `session_manager.cleanup()`
    when done.

    Args:
        mcp_config: MCP configuration manager. If None, loads from default path.

    Returns:
        Tuple of (tools_list, session_manager, server_infos) where:
            - tools_list: List of LangChain `BaseTool` objects
            - session_manager: `MCPSessionManager` instance (call `cleanup()` when done)
            - server_infos: List of `MCPServerInfo` with per-server metadata

    Raises:
        RuntimeError: If an MCP server fails to spawn or connect.
    """
    from langchain_mcp_adapters.tools import load_mcp_tools

    if mcp_config is None:
        mcp_config = MCPConfig()

    servers = mcp_config.list_servers()
    if not servers:
        return [], MCPSessionManager(), []

    # Build connections dict
    connections: dict[str, Connection] = {}
    for name, config in servers.items():
        try:
            connections[name] = build_mcp_server_config(config)
        except ValueError as e:
            logger.warning("Skipping invalid server config %s: %s", name, e)
            continue

    if not connections:
        return [], MCPSessionManager(), []

    # Pre-flight health checks
    errors: list[str] = []
    for server_name, config in servers.items():
        try:
            if config.transport == "stdio":
                _check_stdio_server(server_name, config)
            elif config.transport in _SUPPORTED_REMOTE_TYPES:
                await _check_remote_server(server_name, config)
        except RuntimeError as exc:
            errors.append(str(exc))

    if errors:
        msg = "Pre-flight health check(s) failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise RuntimeError(msg)

    # Create session manager
    manager = MCPSessionManager()

    try:
        client = MultiServerMCPClient(connections=connections)
        manager.client = client
    except Exception as e:
        await manager.cleanup()
        error_msg = f"Failed to initialize MCP client: {e}"
        raise RuntimeError(error_msg) from e

    # Load tools from each server in parallel with a concurrency cap.
    # Pre-enter session contexts sequentially first (fast — just registers
    # cleanup callbacks on the exit stack), then parallelise the actual
    # tool loading (which involves network I/O).
    all_tools: list[BaseTool] = []
    server_infos: list[MCPServerInfo] = []

    # Pre-enter all session contexts so _load_one can use them concurrently
    sessions: dict[str, Any] = {}
    for server_name in [n for n in connections if n in servers]:
        sessions[server_name] = await manager.exit_stack.enter_async_context(
            client.session(server_name)
        )

    _load_semaphore = asyncio.Semaphore(3)

    async def _load_one(
        server_name: str,
        config: MCPServerConfig,
    ) -> tuple[list[BaseTool], MCPServerInfo | None]:
        """Load tools from a single MCP server."""
        async with _load_semaphore:
            session = sessions.get(server_name)
            if session is None:
                return [], None

            tools = await load_mcp_tools(
                session, server_name=server_name, tool_name_prefix=True
            )
            server_info = MCPServerInfo(
                name=server_name,
                transport=config.transport,
                tools=[
                    MCPToolInfo(name=t.name, description=t.description or "")
                    for t in tools
                ],
            )
            return tools, server_info

    tasks = [
        _load_one(name, config)
        for name, config in servers.items()
        if name in connections
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            errors.append(str(r))
        else:
            tools, server_info = r
            all_tools.extend(tools)
            if server_info is not None:
                server_infos.append(server_info)

    if errors:
        logger.warning(
            "MCP server loading completed with %d error(s): %s",
            len(errors),
            "; ".join(errors),
        )

    return all_tools, manager, server_infos


__all__ = [
    "Connection",
    "MCPConfig",
    "MCPServerConfig",
    "MCPServerInfo",
    "MCPSessionManager",
    "MCPToolInfo",
    "MultiServerMCPClient",
    "build_mcp_config_dict",
    "build_mcp_server_config",
    "check_server_connection",
    "create_mcp_client",
    "discover_mcp_configs",
    "load_mcp_config_lenient",
    "load_mcp_tools_with_session",
    "merge_mcp_configs",
]
