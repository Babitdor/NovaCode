"""Research subagents must write findings to files, not just reply inline."""

import pytest

from novacode_cli.agents.default_subagents.prompt import (
    FACT_CHECKER,
    FINANCIAL_ANALYST,
    LITERATURE_REVIEWER,
    MARKET_ANALYST,
    RESEARCH_SYNTHESIZER,
    TECHNICAL_RESEARCHER,
    WEB_RESEARCHER,
)
from novacode_cli.prompts import render_template

_RESEARCH_SUBAGENTS = {
    "web-researcher": WEB_RESEARCHER,
    "literature-reviewer": LITERATURE_REVIEWER,
    "market-analyst": MARKET_ANALYST,
    "financial-analyst": FINANCIAL_ANALYST,
    "technical-researcher": TECHNICAL_RESEARCHER,
    "fact-checker": FACT_CHECKER,
    "research-synthesizer": RESEARCH_SYNTHESIZER,
}


@pytest.mark.parametrize("name,cfg", list(_RESEARCH_SUBAGENTS.items()))
def test_subagent_has_write_file_tool(name, cfg):
    # ``_filter_tools`` treats a non-empty list as a restrictive whitelist, so a
    # research agent whose prompt mandates write_file must name it explicitly.
    # An EMPTY list is the "inherit every tool" case, which already includes it.
    if not cfg["tools"]:
        return
    assert "write_file" in cfg["tools"], f"{name} lacks write_file"


@pytest.mark.parametrize("name,cfg", list(_RESEARCH_SUBAGENTS.items()))
def test_subagent_prompt_preserves_assigned_files_without_inventing_a_destination(name, cfg):
    prompt = cfg["prompt"].lower()
    assert "if an output file is assigned" in prompt, f"{name} lacks assigned-file handling"
    assert "write_file" in prompt, f"{name} prompt doesn't mention write_file"
    assert "edit_file" in prompt, f"{name} lacks existing-file handling"
    assert "file is the deliverable when requested" in prompt
    assert "report the blocker" in prompt
    assert "do not invent a mandatory" in prompt


def test_orchestrator_requires_files_and_recovers_missing():
    r = render_template(
        "research_swarm.jinja",
        mode="general",
        research_query="q",
        base_dir=".nova/research",
        agents=["web-researcher"],
        fast_mode=False,
        mode_description="x",
        agent_count=1,
        conversation_context="",
    )
    # Mandatory write instruction in task descriptions.
    assert "file is the deliverable" in r.lower() and "write_file" in r
    # Phase 2.5 recovers when a subagent replied inline instead of writing.
    # Assert the recovery behaviour, not one exact phrasing of it.
    low = r.lower()
    assert "phase 2.5" in low
    assert "diagnose" in low
    assert "targeted retry or direct fallback" in low
    assert "empty" in low
