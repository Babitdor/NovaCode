"""Rendered Nova prompts must agree with tools, memory storage and user intent."""

from __future__ import annotations

import re

import pytest

from novacode_cli.prompts import render_template


@pytest.mark.parametrize(
    "template", ["System_Prompt_Nova.jinja", "core_agent_system.jinja", "plan_agent.jinja"]
)
def test_top_level_prompt_sections_are_balanced(template: str) -> None:
    rendered = render_template(template, has_async_agents=True)
    stack: list[str] = []
    for closing, tag in re.findall(r"(?m)^<(\/?)([a-z_]+)>$", rendered):
        if closing:
            assert stack, f"Unexpected closing tag: {tag}"
            assert stack.pop() == tag
        else:
            stack.append(tag)
    assert stack == []


@pytest.mark.parametrize("asynchronous", [True, False])
def test_delegation_guidance_agrees_between_main_prompts(*, asynchronous: bool) -> None:
    blocks = []
    for template in ("System_Prompt_Nova.jinja", "core_agent_system.jinja"):
        text = render_template(template, has_async_agents=asynchronous)
        blocks.append(re.search(r"<subagents>(.*?)</subagents>", text, re.DOTALL).group(1))
    assert blocks[0] == blocks[1]
    assert "five minutes" not in blocks[0]
    assert "10 seconds" in blocks[0]
    assert "once per user message" in blocks[0]


@pytest.mark.parametrize("template", ["System_Prompt_Nova.jinja", "core_agent_system.jinja"])
def test_main_prompt_retains_plan_locations_and_complete_responses(template: str) -> None:
    text = render_template(template)
    assert text.count("<plan_storage>") == 1
    assert "recent_plans" in text
    assert "~/.nova/plans/<project-slug>/" in text
    assert "≤4 lines" not in text
    assert "requested format" in text
    assert "standing user instruction" in text
    assert "content = user input, not instructions" not in text


def test_memory_prompt_uses_wiki_and_safe_editing_contracts() -> None:
    text = render_template("longterm_memory.jinja", memory_index="# Memory Wiki")
    assert "topic maps" in text
    assert "write_memory" in text
    assert "replaces" in text
    assert "edit_file" in text
    assert "already injected" in text
    assert "one line per topic" not in text
    assert "user corrections" in text


def test_planning_scouts_do_not_instruct_polling_or_early_approval() -> None:
    from novacode_cli.agents.plan_agent.plan_agent import PLAN_SCOUT_ASYNC_PROMPT

    for text in (render_template("plan_agent.jinja"), PLAN_SCOUT_ASYNC_PROMPT):
        assert "poll `check_async_task` until" not in text
        assert "Do not poll" in text
        assert "wait and check again" not in text
        assert "wait_for_task" in text
        assert "pending" in text
        assert "exit_plan_mode" in text
