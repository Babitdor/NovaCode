"""/research dispatches seven personas by name; the graph must actually have them.

They were once deleted from the roster as "niche, unreferenced anywhere in the
codebase" — but prompts/research_swarm.jinja references all seven, so the
command silently broke: the orchestrator asked for subagent types that no longer
existed. The objection behind the deletion was real though (each description
costs ~30 tokens in the `task` schema on EVERY turn), so they are registered but
deferred: present in the graph, absent from the description.
"""

from __future__ import annotations

import re
from pathlib import Path

from novacode_cli.agents.default_subagents.subagents import (
    RESEARCH_SWARM_AGENTS,
    retrieve_core_subagents,
)

_SWARM_PROMPT = Path("novacode_cli/prompts/research_swarm.jinja")


def _roster() -> set[str]:
    return {a["name"] for a in retrieve_core_subagents([])}


def test_every_agent_the_swarm_prompt_names_is_registered():
    """The regression: the prompt named seven agents the graph did not have."""
    missing = sorted(RESEARCH_SWARM_AGENTS - _roster())
    assert not missing, f"/research would dispatch non-existent subagents: {missing}"


def test_the_deferred_set_matches_what_the_prompt_actually_uses():
    """Keeps the two from drifting apart the way they did before."""
    text = _SWARM_PROMPT.read_text(encoding="utf-8")
    named = {a for a in RESEARCH_SWARM_AGENTS if re.search(rf"\b{re.escape(a)}\b", text)}
    assert named == set(RESEARCH_SWARM_AGENTS), (
        "RESEARCH_SWARM_AGENTS lists agents the prompt never dispatches: "
        f"{sorted(set(RESEARCH_SWARM_AGENTS) - named)}"
    )


def test_they_cost_nothing_in_the_task_description():
    """Registered but hidden — otherwise this fix re-bills every ordinary turn."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage

    from novacode_cli.agents.tool_search import ToolSearchMiddleware

    specs = [a for a in retrieve_core_subagents([]) if a["name"] in RESEARCH_SWARM_AGENTS]
    mw = ToolSearchMiddleware(
        deferred_subagents={s["name"]: s.get("description", "") for s in specs}
    )

    from langchain_core.tools import StructuredTool

    listing = "\n".join(f"- {s['name']}: {s.get('description', '')}" for s in specs)
    task = StructuredTool.from_function(
        lambda description: description,  # noqa: ARG005
        name="task",
        description=f"Delegate to a subagent.\n{listing}",
    )
    req = ModelRequest(
        model=None,
        tools=[task],
        system_prompt=None,
        messages=[HumanMessage("hi")],
        tool_choice=None,
        state={},
        runtime=None,
    )
    desc = next(t for t in mw._apply(req).tools if t.name == "task").description

    for name in RESEARCH_SWARM_AGENTS:
        assert f"- {name}:" not in desc, f"{name} is still billed on every turn"
    assert f"{len(RESEARCH_SWARM_AGENTS)} more specialist subagents" in desc


def test_the_browser_agent_is_granted_playwright_and_cua_tools():
    """Both MCP servers, matched by tool-name prefix, and nothing from the others."""
    from types import SimpleNamespace

    from novacode_cli.agents.core_agent import MCP_TOOLS_BY_SUBAGENT, _grant_mcp_tools

    assert MCP_TOOLS_BY_SUBAGENT["browser-automation-agent"] == ("playwright", "cua-driver")

    def tool(name):  # noqa: ANN001, ANN202
        return SimpleNamespace(name=name)

    mcp = [tool(n) for n in ("playwright_browser_click", "cua-driver_click", "serena_find_symbol", "apify_call-actor")]
    specs = [
        {"name": "browser-automation-agent", "tools": [tool("fetch_url")]},
        {"name": "bug-fix-agent", "tools": []},
    ]
    _grant_mcp_tools(specs, mcp)
    assert [t.name for t in specs[0]["tools"]] == ["fetch_url", "playwright_browser_click", "cua-driver_click"]
    assert specs[1]["tools"] == [], "an agent absent from the map gets no MCP tools"

    _grant_mcp_tools(specs, mcp)  # the specs are cached and reused: must not double up
    assert len(specs[0]["tools"]) == 3


def test_the_browser_agent_loads_the_skills_that_teach_driving_a_browser():
    from novacode_cli.agents.default_subagents.subagents import retrieve_core_subagents

    agent = next(s for s in retrieve_core_subagents([]) if s["name"] == "browser-automation-agent")
    assert agent["skills"] == ["/skills/agent-browser/", "/skills/browser-use/", "/skills/web-research/"]
    assert "cua-driver_" in agent["system_prompt"] and "playwright_browser_" in agent["system_prompt"]
