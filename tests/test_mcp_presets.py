"""Preset catalog compatibility and installation configuration checks."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from novacode_cli.mcp.presets import MCP_PRESETS, create_config_from_preset


@pytest.mark.parametrize("name", list(MCP_PRESETS))
def test_preset_installation_resolves_inputs_without_mutating_template(name: str):
    preset = MCP_PRESETS[name]
    before = deepcopy(preset)
    inputs = {preset["setup_key"]: "example-value"} if "setup_key" in preset else {}
    with patch("novacode_cli.mcp.config.shutil.which", return_value="mock-binary"):
        config = create_config_from_preset(name, inputs)
    assert config is not None
    assert config.command is None or "{" not in config.command
    assert all("{" not in arg for arg in config.args)
    assert all("{" not in value for value in config.env.values())
    assert config.description == preset["description"]
    assert preset == before


def test_catalog_keeps_configured_servers_and_excludes_retired_presets():
    assert {"playwright", "serena", "apify", "cua-driver", "perplexity"} <= MCP_PRESETS.keys()
    assert (
        not {
            "github",
            "laya-browser",
            "brave-search",
            "postgres",
            "sqlite",
            "fetch",
            "time",
            "memory",
            "filesystem",
            "stripe",
            "everything",
        }
        & MCP_PRESETS.keys()
    )


def test_perplexity_uses_official_package_and_api_key_environment():
    with patch("novacode_cli.mcp.config.shutil.which", return_value="mock-binary"):
        config = create_config_from_preset("perplexity", {"perplexity_api_key": "test-api-key"})
    assert config.command == "npx"
    assert config.args == ["-y", "@perplexity-ai/mcp-server"]
    assert config.env == {"PERPLEXITY_API_KEY": "test-api-key"}


def test_cua_executable_path_with_spaces():
    with patch("novacode_cli.mcp.config.Path.is_file", return_value=True):
        config = create_config_from_preset(
            "cua-driver", {"cua_driver_path": r"C:\Program Files\Cua\cua-driver.exe"}
        )
    assert config.command == r"C:\Program Files\Cua\cua-driver.exe"
    assert config.args == ["mcp"]


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("figma", "https://mcp.figma.com/mcp"),
        ("vercel", "https://mcp.vercel.com"),
        ("higgsfield", "https://mcp.higgsfield.ai/mcp"),
    ],
)
def test_hosted_design_presets_use_oauth_without_local_packages(name: str, url: str):
    from novacode_cli.mcp.client import build_mcp_server_config

    config = create_config_from_preset(name)
    assert config is not None
    assert config.transport == "http"
    assert config.url == url
    assert config.oauth is True
    assert config.command is None
    assert config.env == {}
    assert config.header_secrets == {}
    with patch("novacode_cli.mcp.auth.oauth_provider", return_value="oauth-provider"):
        connection = build_mcp_server_config(config)
    assert connection["auth"] == "oauth-provider"
    assert connection["url"] == url


def test_figma_desktop_uses_local_server_without_oauth():
    config = create_config_from_preset("figma-desktop")
    assert config is not None
    assert config.url == "http://127.0.0.1:3845/mcp"
    assert config.oauth is False


@pytest.mark.parametrize("name", ["figma", "vercel", "higgsfield", "figma-desktop"])
def test_hosted_preset_cli_install_does_not_prompt_or_install_packages(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.mcp import commands
    from novacode_cli.mcp.config import MCPConfig

    path = tmp_path / "mcp.json"
    monkeypatch.setattr(commands, "MCPConfig", lambda: MCPConfig(path))
    with (
        patch("prompt_toolkit.PromptSession") as session,
        patch.object(commands, "_auto_install_package") as install,
        patch.object(commands, "_install_npm_package") as npm_install,
    ):
        commands._install(name)
    session.assert_not_called()
    install.assert_not_called()
    npm_install.assert_not_called()
    config = MCPConfig(path).get_server(name)
    assert config is not None
    assert config.oauth is (name != "figma-desktop")


@pytest.mark.parametrize(
    ("name", "service", "port"),
    [("google-drive", "drive", 8001), ("gmail", "gmail", 8002), ("google-docs", "docs", 8003)],
)
def test_google_workspace_presets_scope_services_and_resolve_oauth_file(
    name: str, service: str, port: int
):
    oauth_file = r"C:\My Google OAuth\client_secret.json"
    with patch("novacode_cli.mcp.config.shutil.which", return_value="mock-binary"):
        config = create_config_from_preset(name, {"google_client_secret_path": oauth_file})
    assert config is not None
    assert config.command == "uvx"
    assert config.args == [
        "workspace-mcp",
        "--transport",
        "stdio",
        "--tools",
        service,
        "--tool-tier",
        "core",
    ]
    assert config.env["GOOGLE_CLIENT_SECRET_PATH"] == oauth_file
    assert config.env["WORKSPACE_MCP_PORT"] == str(port)
    assert config.env["GOOGLE_OAUTH_REDIRECT_URI"] == f"http://localhost:{port}/oauth2callback"
    assert config.env["MCP_ENABLE_OAUTH21"] == "false"


def test_vercel_preset_limits_its_tools_to_a_core_set():
    config = create_config_from_preset("vercel", {})
    assert config is not None
    assert config.tools is not None
    assert {"list_projects", "get_deployment", "get_runtime_logs"} <= set(config.tools)
    assert len(config.tools) <= 25


def test_server_allowlist_filters_tools_before_registration(tmp_path: Path):
    import json

    from mcp.types import Tool

    from novacode_cli.mcp.middleware import MCPMiddleware

    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "big": {"transport": "http", "url": "https://x.test/mcp", "tools": ["list_*"]},
                    "all": {"transport": "http", "url": "https://y.test/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    middleware = MCPMiddleware(config_path)
    raw = [
        Tool(name=n, inputSchema={"type": "object"})
        for n in ("list_projects", "update_project", "list_teams")
    ]
    connection = {"transport": "streamable_http", "url": "https://x.test/mcp"}
    kept = middleware._convert_raw_tools("big", connection, raw)
    assert sorted(t.name for t in kept) == ["big_list_projects", "big_list_teams"]
    everything = middleware._convert_raw_tools("all", connection, raw)
    assert len(everything) == 3
