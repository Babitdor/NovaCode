"""Combining built-in, named, plugin and async agents must leave unique names."""

from novacode_cli.agents.default_subagents.subagents import (
    retrieve_core_subagents,
    unique_subagent_specs,
)


def test_duplicate_code_explorer_keeps_builtin_and_preserves_input() -> None:
    builtin = next(s for s in retrieve_core_subagents([]) if s["name"] == "code-explorer")
    duplicate = {**builtin, "description": "Duplicate from a named agent"}
    plugin = {**builtin, "name": "plugin-agent"}
    specs = [builtin, duplicate, plugin, {**plugin}]
    result = unique_subagent_specs(specs)
    assert [s["name"] for s in result] == ["code-explorer", "plugin-agent"]
    assert result[0] is builtin
    assert len(specs) == 4
    assert duplicate["description"] == "Duplicate from a named agent"


def test_duplicate_sync_and_async_names_keep_first_registration() -> None:
    sync = {"name": "reviewer", "description": "Sync", "system_prompt": "Review code"}
    remote = {
        "name": "reviewer",
        "description": "Async",
        "graph_id": "review",
        "url": "http://localhost",
    }
    compiled = {"name": "compiled", "description": "Compiled", "runnable": object()}
    result = unique_subagent_specs([sync, remote, compiled, compiled])
    assert result == [sync, compiled]
    assert unique_subagent_specs(result) == result
