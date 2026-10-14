"""Offline stdio peer for preset wiring; never contacts a Google endpoint."""

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

service = sys.argv[1]
server = FastMCP("offline-google-workspace")


@server.tool(name=f"{service}_configuration")
def configuration() -> dict:
    """Report only fixture identity and configured callback routing."""
    path = Path(os.environ["GOOGLE_CLIENT_SECRET_PATH"])
    client = json.loads(path.read_text())["installed"]
    if not isinstance(client.get("client_id"), str) or not client.get("client_secret"):
        raise ValueError("Malformed OAuth fixture")
    return {"service": service, "port": os.environ["WORKSPACE_MCP_PORT"],
            "redirect": os.environ["GOOGLE_OAUTH_REDIRECT_URI"],
            "client_id": client["client_id"]}


if __name__ == "__main__":
    server.run(transport="stdio")
