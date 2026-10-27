"""Small stdio MCP client for one coordinator-bound CLI participant."""

from __future__ import annotations

import asyncio
import json
import os


async def request(operation: str, payload: dict) -> dict:
    """Contact only the loopback endpoint injected by the running coordinator."""
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", int(os.environ["NOVA_ROOMS_PORT"]), limit=1_000_000
    )
    try:
        writer.write(
            json.dumps(
                {
                    "token": os.environ["NOVA_ROOMS_TOKEN"],
                    "operation": operation,
                    "payload": payload,
                },
                ensure_ascii=False,
            ).encode()
            + b"\n"
        )
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 20))
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> None:
    """Expose context and handoffs, with no owner or arbitrary-room operations."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("nova_rooms")

    @server.tool()
    async def read_room() -> dict:
        """Read this room's shared messages and published notes."""
        return await request("read", {})

    @server.tool()
    async def read_board() -> dict:
        """Read published project tasks and memory, without other rooms' DMs."""
        return await request("board", {})

    @server.tool()
    async def publish_note(kind: str, text: str) -> dict:
        """Publish a decision or finding under your bound identity."""
        return await request("publish", {"kind": kind, "text": text})

    @server.tool()
    async def publish_artifact(title: str, text: str) -> dict:
        """Publish a shared text artifact under your identity in this room."""
        return await request("artifact", {"title": title, "text": text})

    @server.tool()
    async def read_artifact(note: str) -> dict:
        """Read a verified artifact by its note ID, restricted to this room."""
        return await request("read_artifact", {"note": note})

    @server.tool()
    async def handoff(
        goal: str, completed: str, files: str, checks: str, questions: str, next_owner: str
    ) -> dict:
        """Hand off within the approved goal and participant list; consumes a task turn."""
        return await request(
            "handoff",
            {
                "goal": goal,
                "completed": completed,
                "files": files,
                "checks": checks,
                "questions": questions,
                "next_owner": next_owner,
            },
        )

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
