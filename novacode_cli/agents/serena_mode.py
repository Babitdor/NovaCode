"""Serena mode: Nova acts through the Serena MCP server's semantic toolset.

Serena (github.com/oraios/serena) is an MCP server that gives an agent
IDE-grade, language-server-backed tools: symbol-level lookup and navigation,
symbol-level editing, pattern search, shell execution, and per-project
memories. In Serena mode Nova's built-in filesystem/shell/web tools step
aside and the model works exclusively through MCP tools — Serena's plus any
other configured servers — with a system-prompt block that re-anchors the
workflow on Serena's symbolic-first discipline.

The mode mirrors Browser Mode's mechanics: a per-session flag read through
the shared agent-loop contextvar, a request-local tool filter (no agent
rebuild), an appended prompt block, and an execution-time fail-closed
rejection for everything the filter removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ToolCallRequest
from langchain_core.messages import SystemMessage, ToolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from langchain.agents.middleware.types import ModelResponse
    from langgraph.types import Command

#: Tools that stay bound beside the MCP schemas. ``MCPTool`` is the generic
#: MCP router (and the only path snippets' ``mcp.*`` namespace routes through),
#: ``eval``/``MCPCode`` are Code Mode's execution surface, and ``task`` keeps
#: subagent delegation (and the task() snippet bridge) alive.
SERENA_KEEP_TOOLS = frozenset({"MCPTool", "eval", "MCPCode", "task"})

SERENA_MODE_PROMPT = """Serena Mode is active. You are operating as Serena: a semantic-code
agent whose entire toolset is MCP. Nova's built-in filesystem, shell, web and
knowledge tools are NOT available in this mode — every capability you have
comes from the bound MCP tools: Serena's language-server-backed semantic tools
plus any other configured MCP servers. Tool names are server-prefixed (for
example serena_find_symbol); use the exact bound names and never guess
arguments — describe a schema before a novel call.

Work the Serena way:
- Navigate symbols before files. Prefer find_symbol / get_symbols_overview /
  find_referencing_symbols to locate and understand code; read whole files
  only when a symbol view is not enough.
- Edit at the symbol level when the tooling allows it (replace_symbol_body,
  insert_before/after_symbol, rename_symbol); fall back to file-level edits
  for non-symbolic changes.
- Use list_dir for directories, find_file to locate a file, and read_file
  only with a confirmed project-relative file path. Use search_for_pattern
  for regex sweeps and execute_shell_command for builds, tests and VCS.
- Serena's thinking checkpoints (think_about_collected_information,
  think_about_task_adherence, think_about_whether_you_are_done) are part of
  its discipline: call them at the moments their names describe when present.
- Project memories are first-class: list_memories/read_memory before working
  in an unfamiliar project, write_memory for durable project knowledge, and
  never delete a memory without explicit user intent.
- If serena_initial_instructions is bound, call it once at the start of a
  coding task: it is Serena's own usage manual and overrides this summary.

Code Mode keeps working in this mode, but only through MCP: inside snippets
use mcp.list()/mcp.describe()/mcp.call(server, tool, args) — the tools.call
namespace does not carry MCP tools. Normal approvals and execution policy
still apply to every MCP call. After edits, run at least one relevant check
through the shell tool and inspect its result before reporting completion.
"""


def serena_mode_enabled() -> bool:
    """Return the invoking session's live mode flag."""
    from novacode_cli.config.plan_mode import active_plan_session

    return getattr(active_plan_session.get(), "serena_mode_enabled", False) is True


def serena_tool_allowed(name: str, metadata: Mapping[str, object] | None = None) -> bool:
    """MCP tools (Serena's and every other configured server's) plus the
    router and Code Mode surfaces; everything else is out of scope."""
    if metadata and ("nova_mcp_server" in metadata or "nova_mcp_tool" in metadata):
        return True
    return name in SERENA_KEEP_TOOLS


def serena_mcp_ready() -> tuple[bool, str]:
    """Whether a Serena MCP server is connected right now, with guidance if not.

    Matches the server case-insensitively by prefix so ``serena``, ``Serena``
    and ``serena-local`` all qualify, and requires a live connection — cached
    schemas without a session would fail on first call.
    """
    from novacode_cli.mcp import get_shared_mcp_middleware

    try:
        middleware = get_shared_mcp_middleware()
    except Exception:  # noqa: BLE001 — no runtime in this process
        return False, (
            "Serena Mode needs the Serena MCP server, but MCP is not initialized "
            "in this session. Start Nova in a session with MCP enabled and try again."
        )
    for server, status in middleware.server_status.items():
        if not server.lower().startswith("serena"):
            continue
        if status == "ready":
            tools = [
                tool
                for tool in middleware.tools
                if (getattr(tool, "metadata", None) or {}).get("nova_mcp_server") == server
            ]
            return True, f"Serena MCP server '{server}' is connected ({len(tools)} tools)."
        if status == "failed":
            reason = middleware.connection_errors.get(server, "unknown error")
            return False, (
                f"The Serena MCP server '{server}' failed to connect: {reason}. "
                "Open /mcp to inspect or retry it, then run /serena again."
            )
        return False, (
            f"The Serena MCP server '{server}' is still {status}. "
            "Wait for it to connect (or check /mcp), then run /serena again."
        )
    return False, (
        "Serena Mode needs the Serena MCP server, but no 'serena' server is "
        "configured. Open /mcp to add it — Serena ships as a ready-made preset — "
        "connect it, then run /serena again."
    )


class SerenaModeMiddleware(AgentMiddleware):
    """Filter schemas to MCP tools and independently block non-MCP execution."""

    @staticmethod
    def _prepare(request: ModelRequest) -> ModelRequest:
        if not serena_mode_enabled():
            return request
        blocks = (
            [dict(block) for block in request.system_message.content_blocks]
            if request.system_message
            else []
        )
        tools = [
            tool
            for tool in request.tools
            if serena_tool_allowed(
                tool.get("name", tool.get("function", {}).get("name", ""))
                if isinstance(tool, dict)
                else tool.name,
                tool.get("metadata") if isinstance(tool, dict) else tool.metadata,
            )
        ]
        return request.override(
            tools=tools,
            system_message=SystemMessage(
                content=[*blocks, {"type": "text", "text": SERENA_MODE_PROMPT}]
            ),
        )

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """Expose only MCP schemas to synchronous models."""
        return handler(self._prepare(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """Use the same request-local restriction for asynchronous models."""
        return await handler(self._prepare(request))

    @staticmethod
    def _rejection(request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call.get("name", "")
        target = getattr(request, "tool", None)
        if not serena_mode_enabled() or serena_tool_allowed(
            name, getattr(target, "metadata", None)
        ):
            return None
        return ToolMessage(
            content=(
                f"Serena Mode blocks {name}. Only MCP tools (Serena's semantic "
                "tools and other configured MCP servers) are available."
            ),
            name=name,
            tool_call_id=request.tool_call.get("id", ""),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Reject guessed and previously loaded non-MCP calls."""
        rejection = self._rejection(request)
        return rejection if rejection is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Keep execution restricted even when a model ignores its visible schemas."""
        rejection = self._rejection(request)
        return rejection if rejection is not None else await handler(request)
