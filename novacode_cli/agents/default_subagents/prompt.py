from novacode_cli.prompts import render_template

# The other built-in specialists are agent files: default_subagents/builtin/*.md,
# copied to ~/.nova/agents/ on first start (config.seed_builtin_agents).


def _load_prompt(template_name: str) -> str:
    """Load a subagent prompt from a Jinja template."""
    return render_template(f"subagents/{template_name}")


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
