"""Generic discovery and routing over the configured MCP tool inventory."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field


class MCPInput(BaseModel):
    """One compact schema for MCP discovery, inspection and execution."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "describe", "call"] = "list"
    server_name: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    result_path: list[str | int] = Field(default_factory=list, max_length=20)
    max_output_chars: int = Field(default=12000, ge=128, le=50000)


def resolve_target(middleware, params: MCPInput):
    """Resolve by recorded server and raw name, never by an ambiguous prefix."""
    for entry in middleware._tools_cache:
        if entry["server"] == params.server_name and entry["skill_alias"] == params.tool_name:
            return next(
                (
                    t
                    for t in middleware.tools
                    if t.name == entry["name"]
                    and (t.metadata or {}).get("nova_mcp_server") == params.server_name
                ),
                None,
            )
    return None


def build_mcp_tool(middleware) -> StructuredTool:
    """Expose metadata; execution must pass through MCPMiddleware's routing hook."""

    def inspect_tools(**kwargs):
        params = MCPInput.model_validate(kwargs)
        if params.action == "list":
            entries = [
                {
                    "server_name": e["server"],
                    "tool_name": e["skill_alias"],
                    "name": e["name"],
                    "description": e["description"][:300],
                }
                for e in middleware._tools_cache
                if not params.server_name or e["server"] == params.server_name
            ]
            return {
                "tools": entries[params.offset : params.offset + params.limit],
                "total": len(entries),
                "offset": params.offset,
            }
        target = resolve_target(middleware, params)
        if target is None:
            return {"error": "Unknown or disabled MCP tool. Use action='list' to discover tools."}
        if params.action == "describe":
            return {
                "server_name": params.server_name,
                "tool_name": params.tool_name,
                "schema": convert_to_openai_tool(target)["function"],
            }
        return {"error": "MCP calls must execute through the agent's MCP middleware."}

    async def ainspect_tools(**kwargs):
        return inspect_tools(**kwargs)

    return StructuredTool.from_function(
        inspect_tools,
        coroutine=ainspect_tools,
        name="MCPTool",
        args_schema=MCPInput,
        description="List configured MCP tools, describe a tool's JSON schema, or call it. "
        "Use exact server_name and raw tool_name from list, then describe before call. "
        "result_path selects structured output before returning it; output is bounded.",
    )


def routed_request(middleware, request):
    """Expand calls before downstream authorization, sandbox and tool middleware."""
    from novacode_cli.security.policy import get_policy

    # Expansion changes the name seen by downstream middleware. Preserve an
    # explicit deny on the router itself, including in auto-approve sessions.
    if get_policy().tool_default("MCPTool") == "deny":
        raise ValueError("Tool action denied by approval policy.")
    params = MCPInput.model_validate(request.tool_call.get("args") or {})
    target = resolve_target(middleware, params)
    if target is None:
        raise ValueError("Unknown or disabled MCP tool. Use MCPTool(action='list').")
    call = {**request.tool_call, "name": target.name, "args": params.arguments}
    return request.override(tool=target, tool_call=call), params


def bounded_result(result: ToolMessage, params: MCPInput) -> ToolMessage:
    """Select structured JSON locally and bound text without losing error status."""
    artifact = getattr(result, "artifact", None)
    value = (
        artifact.get("structured_content", result.content)
        if isinstance(artifact, dict)
        else result.content
    )
    if params.result_path and result.status != "error":
        if isinstance(value, str):
            value = json.loads(value)
        # MCP adapters may return text content blocks containing serialized JSON.
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            if value[0].get("type") == "text":
                value = json.loads(value[0]["text"])
        for part in params.result_path:
            value = value[part]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    truncated = len(text) > params.max_output_chars
    content = json.dumps(
        {"result": text[: params.max_output_chars], "truncated": truncated}, ensure_ascii=False
    )
    return result.model_copy(update={"name": "MCPTool", "content": content, "artifact": None})
