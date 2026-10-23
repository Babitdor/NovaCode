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


def test_main_and_continuation_share_workflow_and_path_contracts() -> None:
    shared = [render_template(name).strip() for name in ("coding_workflow.jinja", "file_paths.jinja")]
    for name in ("core_agent_system.jinja", "System_Prompt_Nova.jinja"):
        text = render_template(name)
        for block in shared:
            assert text.count(block) == 1
        assert "Task size alone does not require a plan-approval pause" in text
        assert "including Windows paths" in text
        assert "Display" in text and "metadata, not source code" in text


def test_all_packaged_templates_parse_and_contain_no_foreign_runtime_placeholders() -> None:
    from pathlib import Path

    from jinja2 import Environment

    root = Path(__file__).parents[1] / "novacode_cli" / "prompts"
    for path in root.rglob("*.jinja"):
        source = path.read_text(encoding="utf-8")
        Environment().parse(source)
        assert "${SHELL_TOOL_NAME}" not in source, path
        assert "${USE_EMBEDDED_TOOLS_FN" not in source, path
        assert "planning specialist for Claude Code" not in source, path


def test_fact_checker_requires_inspected_evidence_and_never_defaults_to_snippet_verification() -> None:
    text = render_template("subagents/fact_checker.jinja")
    assert "A search snippet alone leaves the claim unverified" in text
    assert "directly inspected source" in text
    assert "search snippets are sufficient" not in text
    assert "do not fetch URLs" not in text


@pytest.mark.parametrize("name", ["summarization.jinja", "session_summarization.jinja"])
def test_handoffs_preserve_provenance_pending_work_and_decisive_evidence(name: str) -> None:
    text = render_template(name)
    for term in ("task IDs", "recovery", "credentials", "authorization", "pending"):
        assert term.lower() in text.lower()
    assert "no other source exists" not in text
    assert "Never include raw tool output" not in text


def test_generated_project_instructions_do_not_invent_build_commands() -> None:
    unknown = render_template("init_agents_md.jinja", commands=[])
    assert "have not been established" in unknown
    assert "pip install -e ." not in unknown
    assert "make test" not in unknown
    known = render_template("init_agents_md.jinja", commands=[{"task": "test", "command": "uv run pytest"}])
    assert "uv run pytest" in known
    assert "have not been established" not in known


def test_agent_generator_does_not_promise_missing_tools_or_private_reasoning() -> None:
    text = render_template("agent_generation.jinja", agent_name="reviewer", description="Review code")
    assert "Use only tools exposed by the current runtime" in text
    assert "Do not request or reveal private internal reasoning" in text
    assert "There is no `run_tests` tool" not in text
    assert "500-800 words" not in text
