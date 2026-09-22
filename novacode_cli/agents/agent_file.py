"""Custom subagent files (``agent.md``) and the tools a subagent may be given.

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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Given to every subagent by deepagents' own middleware; not selectable.
ALWAYS_INCLUDED = ("ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute")


def _split(content: str) -> tuple[dict[str, Any], str]:
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
    return [str(t) for t in tools if str(t).strip()]


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
