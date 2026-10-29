"""Skill policy is shared by actual model requests and respects activation intent."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from novacode_cli.prompts import render_template
from novacode_cli.skills.refreshing_middleware import (
    RefreshingSkillsMiddleware,
    SubagentSkillsMiddleware,
)


@pytest.mark.parametrize("middleware_type", [RefreshingSkillsMiddleware, SubagentSkillsMiddleware])
@pytest.mark.parametrize("template", ["core_agent_system.jinja", "System_Prompt_Nova.jinja"])
def test_model_requests_have_one_consistent_skill_policy(
    middleware_type: type[RefreshingSkillsMiddleware], template: str
):
    middleware = middleware_type(backend=None, sources=[])
    original = SystemMessage(content=render_template(template))
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        system_message=original,
        messages=[HumanMessage("Inspect this code")],
        state={},
    )
    prepared = middleware.modify_request(request)
    text = prepared.system_message.text
    assert text.count("<skills_protocol>") == 1
    assert "ambiguous → skip" not in text
    for requirement in (
        "Explicit requests",
        "task intent",
        "exclusions",
        "smallest useful set",
        "skills_load",
        "before implementation",
        "already loaded",
        "compaction",
        "higher-priority instructions",
        "authorization",
        "missing or disabled",
    ):
        assert requirement in text
    assert original.text == render_template(template)


@pytest.mark.parametrize(
    "text",
    [
        "Do not use $review for this task.",
        "Skip $review and answer normally.",
        "Without using $review, explain the code.",
        "Explain what $review does.",
        "Here is an example: $review",
        "The log says $review was loaded.",
        "```\n$review\n```",
    ],
)
def test_mentions_and_negations_do_not_automatically_load_instructions(text: str):
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    middleware._skills = [
        {"name": "review", "path": "/skills/review/SKILL.md", "description": "Review code."}
    ]
    download = Mock(return_value="INSTRUCTIONS MUST NOT BE LOADED")
    middleware._download_skill = download
    update = middleware._pin_named_skills({"messages": [HumanMessage(text)]}, None, {}, None)
    download.assert_not_called()
    assert not update or not update.get("messages")


@pytest.mark.parametrize("text", ["$review", "/review", "Please use $review to inspect this diff."])
def test_clear_invocations_still_load_the_named_skill(text: str):
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    middleware._skills = [
        {"name": "review", "path": "/skills/review/SKILL.md", "description": "Review code."}
    ]
    middleware._download_skill = Mock(return_value="REVIEW INSTRUCTIONS")
    update = middleware._pin_named_skills({"messages": [HumanMessage(text)]}, None, {}, None)
    assert "REVIEW INSTRUCTIONS" in update["messages"][0].content
