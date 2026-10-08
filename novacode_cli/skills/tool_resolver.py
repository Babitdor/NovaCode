"""Fast aliases over the already-discovered MCP inventory; never reconnect servers."""

from __future__ import annotations


def inventory_resolver(tools: list, metadata: list[dict] | None = None):
    """Resolve actual names, server groups, and unambiguous readable aliases."""
    by_name = {tool.name: tool for tool in tools}
    groups: dict[str, list] = {}
    aliases: dict[str, list] = {}
    for entry in metadata or []:
        tool = by_name.get(entry.get("name"))
        if tool is None:
            continue
        server = entry.get("server", "")
        if server:
            groups.setdefault(server, []).append(tool)
        alias = entry.get("skill_alias") or tool.name.removeprefix(f"{server}_")
        aliases.setdefault(alias, []).append(tool)

    def resolve(name: str, runtime) -> list:
        if name in by_name:
            return [by_name[name]]
        if name in groups:
            return list(groups[name])
        # A bare alias shared by multiple servers is ambiguous; use a group
        # or actual name rather than activating the wrong integration.
        matches = aliases.get(name, [])
        return list(matches) if len(matches) == 1 else []

    return resolve
