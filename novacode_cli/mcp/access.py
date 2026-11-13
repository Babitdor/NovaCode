"""Late MCP discovery for subagents with explicit server/tool grants."""

from langchain.agents.middleware import AgentMiddleware


class MCPAccessMiddleware(AgentMiddleware):
    """Expose only the same server/name grants used during graph construction."""

    def __init__(self, source, servers, names):
        self.source = source
        self.servers = tuple(servers)
        self.names = frozenset(names)

    def _tools(self):
        return [
            t
            for t in self.source.tools
            if t.name in self.names
            or any(t.name.startswith(f"{server}_") for server in self.servers)
        ]

    async def awrap_model_call(self, request, handler):
        import asyncio

        await asyncio.shield(self.source.start_background_discovery())
        tools = self._tools()
        names = {t.name for t in tools}
        existing = [
            t
            for t in request.tools
            if getattr(t, "name", None) not in names
            and not (getattr(t, "metadata", None) or {}).get("nova_mcp_server")
        ]
        return await handler(request.override(tools=[*existing, *tools]))

    async def awrap_tool_call(self, request, handler):
        target = next((t for t in self._tools() if t.name == request.tool_call["name"]), None)
        return await handler(request.override(tool=target) if target else request)
