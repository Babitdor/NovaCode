# Built-In Agents for NOVA CLI

from collections.abc import Callable
from typing import Any

from langchain.tools import BaseTool
from deepagents.middleware.subagents import SubAgent

from .prompt import (
    CODE_DOC_AGENT,
    CODE_EXPLORER,
    CODE_SIMPLIFIER,
    REFACTORING_SPECIALIST_AGENT,
    REVIEWER_AGENT,
    SECURITY_AUDITOR_AGENT,
    # Bug fix agent
    BUG_FIX_AGENT,
    # Test agents
    TEST_WRITER_AGENT,
    TESTING_AGENT,
    # Browser automation agent
    BROWSER_AUTOMATION_AGENT,
    # Domain-specific engineering agents
    FRONTEND_AGENT,
    BACKEND_AGENT,
    DOCKER_AGENT,
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

    # Define subagent configurations
    subagent_configs = [
        # Code quality agents
        ("code-doc-Agent", CODE_DOC_AGENT),
        ("code-simplifier-agent", CODE_SIMPLIFIER),
        ("code-explorer", CODE_EXPLORER),
        ("reviewer-agent", REVIEWER_AGENT),
        ("security-auditor-agent", SECURITY_AUDITOR_AGENT),
        ("refactoring-specialist-agent", REFACTORING_SPECIALIST_AGENT),
        # Bug fix agent
        ("bug-fix-agent", BUG_FIX_AGENT),
        # Test agents
        ("test-writer-agent", TEST_WRITER_AGENT),
        ("testing-agent", TESTING_AGENT),
        # Browser automation agent
        ("browser-automation-agent", BROWSER_AUTOMATION_AGENT),
        # Domain-specific engineering agents
        ("frontend-agent", FRONTEND_AGENT),
        ("backend-agent", BACKEND_AGENT),
        ("docker-agent", DOCKER_AGENT),
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

    subagents: list[SubAgent] = [
        {
            "name": name,
            "description": config["description"],
            "system_prompt": config["prompt"],
            "tools": _filter_tools(all_tools, config["tools"]),
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
        # Code quality agents
        "code-doc-Agent": [
            "/skills/code-documentation/",
        ],
        "code-simplifier-agent": [
            "/skills/code-review-expert/",
        ],
        "code-explorer": [
            "/skills/codebase-explorer/",
            "/skills/graphify/",
        ],
        "reviewer-agent": [
            "/skills/code-review-expert/",
        ],
        "security-auditor-agent": [
            "/skills/web-research/",
        ],
        "refactoring-specialist-agent": [
            "/skills/improve-codebase-architecture/",
        ],
        # Bug fix agent
        "bug-fix-agent": [
            "/skills/systematic-debugging/",
        ],
        # Test agents
        "test-writer-agent": [
            "/skills/test-driven-development/",
        ],
        "testing-agent": [
            "/skills/testing-skills/",
            "/skills/webapp-testing/",
        ],
        # Browser automation agent
        # No browser skills here: this agent only has fetch_url +
        # duckduckgo_search, and agent-browser/browser-use teach driving a
        # browser it cannot drive — which is what made it accept impossible
        # tasks. The main agent owns real browser work (playwright_browser_*).
        "browser-automation-agent": [
            "/skills/web-research/",
        ],
        # Domain-specific engineering agents
        "frontend-agent": [
            "/skills/frontend-design/",
            "/skills/expert-css-skills/",
        ],
        "backend-agent": [
            "/skills/backend-dev-guidelines/",
            "/skills/async-python-patterns/",
        ],
        "docker-agent": [
            "/skills/docker-deploy/",
        ],
    }
    for sa in subagents:
        skills = subagent_skills.get(sa["name"])
        if skills:
            sa["skills"] = skills

    return subagents
