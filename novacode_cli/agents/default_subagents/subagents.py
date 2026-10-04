# Built-In Agents for NOVA CLI

from collections.abc import Callable
from typing import Any

from langchain.tools import BaseTool
from deepagents.middleware.subagents import SubAgent

from .prompt import (
    CODE_EXPLORER,
    REFACTORING_SPECIALIST_AGENT,
    BUG_FIX_AGENT,
    BROWSER_AUTOMATION_AGENT,
    # Research-swarm agents (dispatched by /research; see RESEARCH_SWARM_AGENTS)
    WEB_RESEARCHER,
    FACT_CHECKER,
    RESEARCH_SYNTHESIZER,
    LITERATURE_REVIEWER,
    MARKET_ANALYST,
    FINANCIAL_ANALYST,
    TECHNICAL_RESEARCHER,
)

AnyTool = BaseTool | Callable[..., Any]


def _tool_name(t: AnyTool) -> str | None:
    """Return the name of a tool, whether it's a BaseTool instance or a plain callable."""
    return getattr(t, "name", None) or getattr(t, "__name__", None)


def _filter_tools(tools: list[AnyTool], names: list[str]) -> list[AnyTool]:
    """Return the subset of tools whose names appear in `names`."""
    if not names:
        return tools
    name_set = set(names)
    return [t for t in tools if _tool_name(t) in name_set]


#: Registered in the graph but hidden from the ``task`` tool description: only
#: ``/research`` dispatches them, and it names them itself, so paying ~30 tokens
#: each on every ordinary turn buys nothing. ``search_tools`` still surfaces them
#: if the agent goes looking.
RESEARCH_SWARM_AGENTS = frozenset(
    {
        "web-researcher",
        "fact-checker",
        "research-synthesizer",
        "literature-reviewer",
        "market-analyst",
        "financial-analyst",
        "technical-researcher",
    }
)


#: Subagents kept in the ``task`` tool description on every turn. Everything
#: else stays registered in the graph (so ``task(subagent_type=...)`` and
#: ``/research`` still work by name) but is deferred out of the description:
#: a ~90-entry roster is ~19k chars resent on every call, which is exactly the
#: context cost progressive disclosure exists to avoid. ``search_tools`` finds
#: the rest by what they do.
LISTED_SUBAGENTS = frozenset({"general-purpose"})


def retrieve_core_subagents(
    tools: list[AnyTool] | None = None,
) -> list[SubAgent]:
    all_tools: list[AnyTool] = tools or []

    # The in-process specialists. One earns its place here by needing the
    # session: its approvals, its sandbox, or an answer in the same turn. Work
    # that is long and self-contained is an async agent instead
    # (agents/async_agents/), and domain know-how is a skill the main agent loads.
    subagent_configs = [
        ("code-explorer", CODE_EXPLORER),
        ("refactoring-specialist-agent", REFACTORING_SPECIALIST_AGENT),
        ("bug-fix-agent", BUG_FIX_AGENT),
        ("browser-automation-agent", BROWSER_AUTOMATION_AGENT),
        # Research-swarm agents. These were removed once as "niche, unreferenced
        # anywhere in the codebase" — but /research references all seven by name
        # (prompts/research_swarm.jinja), so removing them silently broke that
        # command: the orchestrator dispatched subagent types the graph no
        # longer had. They are back, and the token objection that motivated the
        # removal is answered by RESEARCH_SWARM_AGENTS below: they are registered
        # in the graph but deferred out of the `task` description, so they cost
        # nothing per turn and /research can still name them directly.
        ("web-researcher", WEB_RESEARCHER),
        ("fact-checker", FACT_CHECKER),
        ("research-synthesizer", RESEARCH_SYNTHESIZER),
        ("literature-reviewer", LITERATURE_REVIEWER),
        ("market-analyst", MARKET_ANALYST),
        ("financial-analyst", FINANCIAL_ANALYST),
        ("technical-researcher", TECHNICAL_RESEARCHER),
    ]

    # The model for the subagent role, if the user chose one. Absent means
    # deepagents has every subagent inherit the main agent's model, which is what
    # they all did before per-role models existed.
    #
    # A model *object*, not a 'provider:model' spec string, even though deepagents
    # documents the string form for this field: a string is resolved with
    # langchain's `init_chat_model`, which does not know Nova's own providers, so
    # choosing e.g. `opencode` for this role crashed the whole agent build. See
    # :func:`novacode_cli.config.model_create.build_role_model`.
    from novacode_cli.config.model_create import build_role_model

    subagent_model = build_role_model("subagent")

    subagents: list[SubAgent] = [
        {
            "name": name,
            "description": config["description"],
            "system_prompt": config["prompt"],
            "tools": _filter_tools(all_tools, config["tools"]),
            # Conditionally spread, so the key is simply absent when the role
            # inherits. One shared instance, exactly as the inherited case shares
            # the main agent's model.
            **({"model": subagent_model} if subagent_model else {}),
        }
        for name, config in subagent_configs
    ]

    # Selectively assign 1-2 targeted skills per subagent.
    # SkillsMiddleware is instantiated per-subagent only when `skills` is set,
    # so skipping agents that don't need skills saves middleware overhead and
    # avoids polluting their system prompt with irrelevant skill content.
    # The general-purpose subagent auto-inherits the main agent's skills from
    # create_deep_agent's top-level `skills` parameter — no need to list it here.
    subagent_skills: dict[str, list[str]] = {
        "code-explorer": [
            "/skills/codebase-explorer/",
            "/skills/graphify/",
        ],
        "refactoring-specialist-agent": [
            "/skills/improve-codebase-architecture/",
        ],
        "bug-fix-agent": [
            "/skills/systematic-debugging/",
        ],
        # It drives a real browser through the MCP tools it is granted (playwright
        # and cua-driver: MCP_TOOLS_BY_SUBAGENT in core_agent.py), and, through
        # the `execute` tool every subagent has, the agent-browser CLI and the
        # Playwright scripts these two skills teach. The skills are the fallback
        # when the MCP servers are absent or the browser is locked.
        "browser-automation-agent": [
            "/skills/agent-browser/",
            "/skills/browser-use/",
            "/skills/web-research/",
        ],
    }
    for sa in subagents:
        skills = subagent_skills.get(sa["name"])
        if skills:
            sa["skills"] = skills

    return subagents
