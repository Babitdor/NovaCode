from novacode_cli.prompts import render_template


def _load_prompt(template_name: str) -> str:
    """Load a subagent prompt from a Jinja template."""
    return render_template(f"subagents/{template_name}")


CODE_EXPLORER = {
    "description": "Helps navigate, understand, and query large codebases efficiently.",
    "prompt": _load_prompt("code_explorer.jinja"),
    "tools": [
        "fetch_url",
        "duckduckgo_search",
        "docs_search",
        "package_info",
    ],
}

REFACTORING_SPECIALIST_AGENT = {
    "description": "Identifies code smells (long methods, duplication, dead code), prioritizes technical debt, and creates incremental refactoring plans. Applies design patterns and SOLID principles.",
    "prompt": _load_prompt("refactoring_specialist_agent.jinja"),
    "tools": [],
}

# ── Bug Fix Agent ──────────────────────────────────────────────────────────────

BUG_FIX_AGENT = {
    "description": "Systematically diagnoses and fixes bugs. Reproduces the issue, identifies root cause, applies minimal fix, and adds regression tests.",
    "prompt": _load_prompt("bug_fix_agent.jinja"),
    "tools": [],
}

# ── Test Agents ────────────────────────────────────────────────────────────────

# ── Browser Automation Agent ───────────────────────────────────────────────────

# NOTE: this agent receives `playwright_browser_*` MCP tools via
# MCP_TOOLS_BY_SUBAGENT (see core_agent.py), so it can drive a real browser.
# Its prompt used to describe `browser_automate` and `capture_browser_console`,
# neither of which exists — so it confidently accepted tasks it could not
# perform. MCP tools attach via MCPMiddleware.tools rather than the plain tool
# list, so a subagent only receives them if it is named in that map.
BROWSER_AUTOMATION_AGENT = {
    "description": (
        "Researches and extracts information from the web: plain HTTP (fetch + search) "
        "for static pages, and a real browser (playwright) for JavaScript-rendered "
        "pages, logins, interaction, screenshots, and console/network inspection. "
        "Can also drive the desktop (cua-driver) for native dialogs and other apps."
    ),
    "prompt": _load_prompt("browser_automation_agent.jinja"),
    "tools": [
        "fetch_url",
        "duckduckgo_search",
    ],
}

# ── Domain-Specific Engineering Agents ─────────────────────────────────────────

# ── Research Swarm Agents ──────────────────────────────────────────────────────
# These are NOT in the default subagent roster (see subagents.py) to keep the
# `task` tool schema lean. They remain defined here for the research-swarm
# workflow (research_swarm.jinja) and its tests.

WEB_RESEARCHER = {
    "description": "Searches the web and fetches primary sources to investigate a specific research sub-question. Writes structured findings to a designated file. Use for general web research assignments within a research swarm.",
    "prompt": _load_prompt("web_researcher.jinja"),
    "tools": [
        "web_search",
        "duckduckgo_search",
        "fetch_url",
        "write_file",
    ],
}

FACT_CHECKER = {
    "description": "Verifies the 3 most critical claims from research findings using web search snippets only. Does not read files (content is passed inline) and does not fetch URLs. Writes a QA report. Use after researchers complete their work.",
    "prompt": _load_prompt("fact_checker.jinja"),
    "tools": [
        "web_search",
        "duckduckgo_search",
        "write_file",
    ],
}

RESEARCH_SYNTHESIZER = {
    "description": "Synthesizes all research findings and the QA report (provided inline by the orchestrator) into a single coherent final report. Does not read files or search the web — works only from content in the task description. Use as the final step of a research swarm.",
    "prompt": _load_prompt("research_synthesizer.jinja"),
    "tools": [],
}

LITERATURE_REVIEWER = {
    "description": "Searches academic databases (arXiv, Google Scholar, Semantic Scholar, PubMed) to find and evaluate peer-reviewed sources on a specific sub-question. Use for academic research swarms.",
    "prompt": _load_prompt("literature_reviewer.jinja"),
    "tools": [
        "web_search",
        "duckduckgo_search",
        "docs_search",
        "fetch_url",
        "write_file",
    ],
}

MARKET_ANALYST = {
    "description": "Researches market size, growth rates, competitive landscape, and industry trends for a specific market sub-question. Targets industry reports, investor relations pages, and trade publications. Use for market research swarms.",
    "prompt": _load_prompt("market_analyst.jinja"),
    "tools": [
        "web_search",
        "duckduckgo_search",
        "fetch_url",
        "write_file",
    ],
}

FINANCIAL_ANALYST = {
    "description": "Researches financial statements, earnings data, news sentiment, and risk factors for a specific company or sector sub-question. Targets SEC filings, earnings transcripts, and financial news. Use for stock/financial research swarms.",
    "prompt": _load_prompt("financial_analyst.jinja"),
    "tools": [
        "web_search",
        "duckduckgo_search",
        "fetch_url",
        "write_file",
    ],
}

TECHNICAL_RESEARCHER = {
    "description": "Researches official documentation, GitHub repos, RFCs, and trusted developer resources on a specific technical sub-question. Writes findings with version-accurate, source-backed technical details. Use for technical research swarms.",
    "prompt": _load_prompt("technical_researcher.jinja"),
    "tools": [
        "web_search",
        "duckduckgo_search",
        "docs_search",
        "fetch_url",
        "write_file",
    ],
}
