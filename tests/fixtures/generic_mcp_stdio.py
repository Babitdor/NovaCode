"""Offline MCP peer for discovery, state reuse and structured result selection."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("generic-routing-test")
count = 0


@server.tool()
def increment(amount: int) -> dict:
    """Increment a counter retained by this server process."""
    global count
    count += amount
    return {"count": count, "large": "payload " * 2000}


@server.tool()
def forbidden() -> str:
    """A tool excluded by the server's allowlist."""
    return "should not be reachable"


if __name__ == "__main__":
    server.run(transport="stdio")
