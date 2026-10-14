"""Preset transport contracts, plus the existing local OAuth failure fixtures.

This exercises Nova's wiring against a fake peer; it does not validate the
third-party workspace-mcp server's authenticated Google behavior.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from novacode_cli.mcp.presets import create_config_from_preset


@pytest.mark.parametrize(
    "name,service,port",
    [
        ("google-drive", "drive", 8001),
        ("gmail", "gmail", 8002),
        ("google-docs", "docs", 8003),
    ],
)
async def test_google_presets_real_stdio_fixture_and_account_isolation(
    tmp_path, name, service, port
):
    server = Path(__file__).parent / "fixtures" / "google_workspace_stdio.py"
    identities = []
    for account in ("first", "second"):
        client = tmp_path / f"{account}.json"
        client.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": f"{account}.apps.googleusercontent.com",
                        "client_secret": "fixture-only",
                    }
                }
            )
        )
        with patch("novacode_cli.mcp.config.shutil.which", return_value="offline-binary"):
            config = create_config_from_preset(name, {"google_client_secret_path": str(client)})
        assert config.args[config.args.index("--tools") + 1] == service
        parameters = StdioServerParameters(
            command=sys.executable, args=[str(server), service], env=config.env
        )
        async with asyncio.timeout(30), stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == [f"{service}_configuration"]
                result = await session.call_tool(f"{service}_configuration", {})
                assert not result.isError
                data = json.loads(result.content[0].text)
                assert data["port"] == str(port)
                assert data["redirect"] == f"http://localhost:{port}/oauth2callback"
                identities.append(data["client_id"])
    assert identities == ["first.apps.googleusercontent.com", "second.apps.googleusercontent.com"]
