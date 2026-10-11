"""Session tools reach declarative subagents without mutating cached specs."""

from novacode_cli.agents import core_agent
from novacode_cli.sessions.gateway import SESSION_TOOLS


def test_session_tool_injection_deduplicates_and_keeps_original_spec(monkeypatch):
    monkeypatch.setattr(core_agent, "retrieve_async_subagents", lambda: [])
    original = {"name": "peer", "system_prompt": "original", "tools": [SESSION_TOOLS[0]]}
    result = core_agent._harden_subagent_specs([original])[0]
    names = [tool.name for tool in result["tools"]]
    assert all(names.count(tool.name) == 1 for tool in SESSION_TOOLS)
    assert original["tools"] == [SESSION_TOOLS[0]]
    assert original["system_prompt"] == "original"
    assert all(
        "sender" not in tool.args and "auto_approve" not in tool.args for tool in SESSION_TOOLS
    )


def test_remote_and_compiled_agents_keep_their_own_configuration():
    specs = [{"name": "remote", "url": "opaque"}, {"name": "compiled", "runnable": object()}]
    result = core_agent._harden_subagent_specs(specs)
    assert result[0] is specs[0] and result[1] is specs[1]
