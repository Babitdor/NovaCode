"""Small, transcript-backed skill snapshots and explicit reload signals."""

from __future__ import annotations

import re
from html import escape
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

_reload_generation = 0
SKILL_REFERENCE = re.compile(r"(?<!\S)[/$]([a-z0-9]+(?:-[a-z0-9]+)*)(?![\w/-])")


def request_reload() -> None:
    """Invalidate every skills middleware, including already-built subagents."""
    global _reload_generation  # noqa: PLW0603 — process-wide invalidation signal
    _reload_generation += 1


def reload_generation() -> int:
    """Return the current library invalidation generation."""
    return _reload_generation


def skill_snapshot(skill: dict[str, Any], content: str) -> tuple[str, dict[str, Any]]:
    """Strip frontmatter and attach its optional tool names to a small snapshot."""
    import yaml

    metadata: dict[str, Any] = {}
    body = content
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", content, re.DOTALL)
    if match:
        body = content[match.end() :].strip()
        try:
            parsed = yaml.safe_load(match.group(1))
            metadata = parsed.get("metadata", {}) if isinstance(parsed, dict) else {}
        except yaml.YAMLError:
            pass
    include = metadata.get("include_tools", "") if isinstance(metadata, dict) else ""
    snapshot = {key: str(skill.get(key, "")) for key in ("name", "path", "description")}
    snapshot["include_tools"] = include.split() if isinstance(include, str) else []
    return body, snapshot


def pinned_message(skill: dict[str, Any], content: str) -> HumanMessage:
    """Create the append-only instruction snapshot recognized by the TUI."""
    body, snapshot = skill_snapshot(skill, content)
    return HumanMessage(
        content=(
            f'<skill name="{escape(snapshot["name"], quote=True)}" '
            f'path="{escape(snapshot["path"], quote=True)}">\n{body}\n</skill>'
        ),
        additional_kwargs={"lc_source": "pinned_skill", "skill": snapshot},
    )


def active_skill_names(messages: list[Any], skills: list[dict[str, Any]]) -> set[str]:
    """Only successful loads still in context activate a skill's private tools."""
    by_path = {str(skill["path"]).replace("\\", "/"): skill["name"] for skill in skills}
    known = {skill["name"]: str(skill["path"]) for skill in skills}
    calls: dict[str, str] = {}
    active: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call.get("name") == "read_file":
                    args = call.get("args") or {}
                    path = str(args.get("file_path", args.get("path", ""))).replace("\\", "/")
                    if path in by_path:
                        calls[call["id"]] = by_path[path]
        if isinstance(message, HumanMessage):
            snapshot = message.additional_kwargs.get("skill", {})
            if message.additional_kwargs.get("lc_source") != "pinned_skill":
                continue
        elif isinstance(message, ToolMessage):
            if message.status == "error" or str(message.content).lstrip().lower().startswith(
                "error"
            ):
                continue
            name = calls.get(message.tool_call_id)
            if name:
                active.add(name)
            artifact = message.artifact if message.name == "skills_load" else None
            snapshot = artifact.get("skill", {}) if isinstance(artifact, dict) else {}
        else:
            continue
        if (
            isinstance(snapshot, dict)
            and snapshot.get("name") in known
            and known[snapshot["name"]] == snapshot.get("path")
        ):
            active.add(snapshot["name"])
    return active
