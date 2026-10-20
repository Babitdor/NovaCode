"""Custom subagent definitions and the tools a subagent may be given.

An ``agent.md`` is YAML frontmatter plus the system prompt::

    ---
    color: "#0ea5e9"
    description: "Reviews Python for security bugs"
    tools: ["web_search", "serena_find_symbol"]
    ---
    You are ...

``tools`` is optional. Absent means every tool (what agents always had);
a list means exactly those, on top of the file/shell tools every subagent gets
from deepagents itself (:data:`ALWAYS_INCLUDED`).

Create new definitions as flat ``.nova/agents/<name>.md`` files in a project or
``~/.nova/agents/<name>.md`` globally. Existing ``<name>/agent.md`` folders and
Claude Code agent files remain supported::

    ---
    name: approver
    description: Final gate; writes decision.md.
    tools: Read, Grep, Glob, Bash, Write
    model: opus
    ---

Claude tool names map to Nova's (:data:`CLAUDE_TOOL_NAMES`). A list that names
any file/shell tool is a Claude-style allowlist: the file/shell tools it leaves
out are taken away (:class:`FilesystemToolFilter`), so ``approver`` has no
``edit_file``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain.tools.tool_node import ToolCallRequest
    from langgraph.types import Command

#: Given to every subagent by deepagents' own middleware; not selectable.
ALWAYS_INCLUDED = (
    "ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute",
)

#: Claude Code tool names in an agent file's ``tools:`` → Nova's tool names.
CLAUDE_TOOL_NAMES = {
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "edit_file",
    "Grep": "grep",
    "Glob": "glob",
    "LS": "ls",
    "Bash": "execute",
    "WebFetch": "fetch_url",
    "WebSearch": "web_search",
    "TodoWrite": "write_todos",
}


def _parse(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    front: dict[str, Any] = {}
    try:
        import yaml

        loaded = yaml.safe_load(parts[1])
        if isinstance(loaded, dict):
            front = loaded
    except Exception:  # noqa: BLE001 — hand-written files: fall back to key: value lines
        for line in parts[1].splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                front[key.strip()] = value.strip().strip('"').strip("'")
    return front, parts[2].lstrip("\n")


def _split(content: str) -> tuple[dict[str, Any], str]:
    """Reuse parsed frontmatter by exact content, never resolved capabilities."""
    from time import perf_counter_ns

    from novacode_cli.computation_cache import digest, get, put, record_timing

    started = perf_counter_ns()
    key = digest("agent-definition-v1\0" + content)
    record_timing("agent_definitions", "validation", perf_counter_ns() - started)
    cached = get("agent_definitions", key)
    if cached is not None:
        cached = cast("list[Any]", cached)
        return cached[0], cached[1]
    started = perf_counter_ns()
    front, body = _parse(content)
    record_timing("agent_definitions", "computation", perf_counter_ns() - started)
    if set(front) <= {"name", "description", "tools", "model", "color", "skill_names"}:
        put("agent_definitions", key, [front, body])
    return front, body


def read_agent(path: Path) -> tuple[dict[str, Any], str]:
    """``(frontmatter, system_prompt)`` of an ``agent.md``."""
    return _split(path.read_text(encoding="utf-8"))


def agent_tools(content: str) -> list[str] | None:
    """The agent's chosen tool names, or None when it did not choose (= all)."""
    tools = _split(content)[0].get("tools")
    if tools is None:
        return None
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",")]
    names = [str(t).strip() for t in tools if str(t).strip()]
    return list(dict.fromkeys(CLAUDE_TOOL_NAMES.get(n, n) for n in names))


def filesystem_allowlist(chosen: list[str] | None) -> set[str] | None:
    """The file/shell tools an agent keeps, or None when it keeps all of them.

    Only a list naming at least one of them restricts: Nova's own agent files
    never list them (the /agents picker hides them), so they keep every one.
    """
    allowed = set(chosen or ()) & set(ALWAYS_INCLUDED)
    return allowed or None


class FilesystemToolFilter(AgentMiddleware):
    """Takes away the file/shell tools an agent's ``tools:`` list left out.

    deepagents gives every subagent all of :data:`ALWAYS_INCLUDED`; this hides
    the others from the model and refuses a call to one anyway.
    """

    def __init__(self, allowed: set[str]) -> None:
        """Keep *allowed* of :data:`ALWAYS_INCLUDED`; block the rest."""
        super().__init__()
        self._blocked = set(ALWAYS_INCLUDED) - allowed

    def _bind(self, request: ModelRequest) -> ModelRequest:
        keep = [t for t in request.tools if getattr(t, "name", None) not in self._blocked]
        return request.override(tools=keep)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call
        if call["name"] not in self._blocked:
            return None
        return ToolMessage(
            f"{call['name']} is not in this agent's tools.",
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Bind only the allowed file/shell tools."""
        return handler(self._bind(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Bind only the allowed file/shell tools."""
        return await handler(self._bind(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Refuse a blocked tool the model called anyway."""
        return self._refusal(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Refuse a blocked tool the model called anyway."""
        return self._refusal(request) or await handler(request)


def write_agent(path: Path, front: dict[str, Any], body: str) -> None:
    """Write ``agent.md``. Values are JSON-quoted, which is valid YAML: a
    description holding ``: `` or a Windows path can no longer break it."""
    lines = ["---"]
    for key, value in front.items():
        if value is None:
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines += ["---", "", body.strip(), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def set_agent_tools(path: Path, tools: list[str] | None) -> None:
    """Replace an agent's tool selection (None = back to every tool)."""
    front, body = read_agent(path)
    front.pop("tools", None)
    if tools is not None:
        front["tools"] = sorted(tools)
    write_agent(path, front, body)


def tool_arsenal(session_tools: list[Any] | None = None) -> list[tuple[str, str]]:
    """Every selectable tool as ``(name, one-line description)``, sorted.

    Nova's own tools come from the live session; MCP tools from the schema
    cache of each enabled server (named ``<server>_<tool>``, as bound).
    """
    found: dict[str, str] = {}
    for tool in session_tools or []:
        name = getattr(tool, "name", None)
        if name and name not in ALWAYS_INCLUDED:
            found[name] = (getattr(tool, "description", "") or "").strip().split("\n")[0]
    try:
        from novacode_cli.mcp.config import MCPConfig
        from novacode_cli.mcp.middleware import _load_schema_cache

        enabled = {
            name for name, cfg in MCPConfig().list_servers().items()
            if not getattr(cfg, "disabled", False)
        }
        for server, entry in _load_schema_cache().items():
            if server not in enabled or not isinstance(entry, dict):
                continue
            for raw in entry.get("tools") or []:
                desc = (raw.get("description") or "").strip().split("\n")[0]
                found[f"{server}_{raw.get('name')}"] = desc
    except Exception:  # noqa: BLE001 — MCP listing is best effort
        pass
    return sorted(found.items())
