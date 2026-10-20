# Built-In Agents for NOVA CLI

import logging
from collections.abc import Callable
from typing import Any

from deepagents.middleware.subagents import SubAgent
from langchain.tools import BaseTool

from .prompt import (
    FACT_CHECKER,
    FINANCIAL_ANALYST,
    LITERATURE_REVIEWER,
    MARKET_ANALYST,
    RESEARCH_SYNTHESIZER,
    TECHNICAL_RESEARCHER,
    # Research-swarm agents (dispatched by /research; see RESEARCH_SWARM_AGENTS)
    WEB_RESEARCHER,
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


def unique_subagent_specs(specs: list[SubAgent]) -> list[SubAgent]:
    """Keep the first name per tool namespace without mutating cached specs."""
    seen: set[tuple[str, bool]] = set()
    result: list[SubAgent] = []
    for spec in specs:
        name = spec["name"]
        key = (name, "graph_id" in spec)
        if key in seen:
            logging.getLogger(__name__).warning(
                "Skipping duplicate subagent registration: %s", name
            )
            continue
        seen.add(key)
        result.append(spec)
    return result


def retrieve_core_subagents(
    tools: list[AnyTool] | None = None,
) -> list[SubAgent]:
    all_tools: list[AnyTool] = tools or []

    # Only the research swarm stays in code: /research names it directly. The
    # other in-process specialists are agent files in ~/.nova/agents/ (shipped
    # in default_subagents/builtin/), loaded by build_named_subagents.
    subagent_configs = [
        # Research-swarm agents. These were removed once as "niche, unreferenced
        # anywhere in the codebase" — but /research references all seven by name
        # (prompts/research_swarm.jinja), so removing them silently broke that
        # command: the orchestrator dispatched subagent types the graph no
        # longer had. They cost nothing per turn: RESEARCH_SWARM_AGENTS keeps
        # them registered but deferred out of the `task` description.
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

    from novacode_cli.skills.libraries import SPECIALIST_LIBRARIES

    # Names resolve across every precedence root; individual skill directories
    # are not library roots and must not be passed as the upstream `skills`.
    for sa in subagents:
        sa["skill_names"] = list(SPECIALIST_LIBRARIES.get(sa["name"], ()))

    return subagents
