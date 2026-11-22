"""Tests for live steering injection.

Steering must (a) land in the system prompt as a standing instruction AND (b)
surface a newly-added steer as a one-time live user message so the agent —
mid-tool-loop — actually reads and acts on it instead of ignoring a passive
system-prompt line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import HumanMessage

from novacode_cli.bootstrap.steering import SteeringInstruction, SteeringMiddleware

if TYPE_CHECKING:
    from collections.abc import Iterator


def test_late_live_message_continues_before_finishing_the_turn():
    from langchain_core.messages import AIMessage

    instructions = []
    middleware = SteeringMiddleware(instructions=instructions)
    instructions.append(SteeringInstruction("steer", "Use the corrected filename", live=True))
    update = middleware.after_model({"messages": [AIMessage("Initial answer")]}, None)
    assert update["jump_to"] == "model"
    assert "corrected filename" in update["messages"][0].content
    assert instructions[0].consumed
    assert middleware.after_model({"messages": [AIMessage("Corrected answer")]}, None) is None


def test_live_message_during_a_tool_call_waits_for_paired_results():
    from langchain_core.messages import AIMessage

    instruction = SteeringInstruction("steer", "Check the new requirement", live=True)
    middleware = SteeringMiddleware(instructions=[instruction])
    response = AIMessage("", tool_calls=[{"id": "c1", "name": "read_file", "args": {}}])
    assert middleware.after_model({"messages": [response]}, None) is None
    assert not instruction.consumed
    assert middleware.before_model({}, None)["messages"]


@pytest.mark.asyncio
async def test_real_graph_delivers_input_arriving_inside_the_final_model_call():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    instructions = []

    def responses() -> Iterator[AIMessage]:
        instructions.append(SteeringInstruction("steer", "Use the corrected filename", live=True))
        yield AIMessage("Initial answer")
        yield AIMessage("Corrected answer")

    agent = create_agent(
        GenericFakeChatModel(messages=responses()),
        middleware=[SteeringMiddleware(instructions=instructions)],
    )
    result = await agent.ainvoke({"messages": [HumanMessage("Start the task")]})
    assert result["messages"][-1].content == "Corrected answer"
    queued = [
        message for message in result["messages"]
        if message.additional_kwargs.get("lc_source") == "queued_message"
    ]
    assert len(queued) == 1
    assert "corrected filename" in queued[0].content


class _Req:
    """Minimal stand-in for langchain's ModelRequest (only what _inject uses)."""

    def __init__(self, system_prompt="BASE", messages=None):
        self.system_prompt = system_prompt
        self.messages = messages if messages is not None else []
        self.applied: dict = {}

    def override(self, **kw):
        out = _Req(self.system_prompt, kw.get("messages", self.messages))
        out.applied = kw
        return out


def _content(m):
    return str(getattr(m, "content", m))


def test_steer_in_system_prompt_and_delivered_as_message_once():
    instrs = [SteeringInstruction("steer", "focus on performance")]
    mw = SteeringMiddleware(instructions=instrs, treat_existing_as_new=True)

    out1 = mw._inject(_Req("BASE", [HumanMessage("hi")]))
    sm = out1.applied["system_message"]
    assert "User Steering" in sm.content
    assert "focus on performance" in sm.content
    # Newly added -> surfaced as a live user message.
    assert "messages" in out1.applied
    last = out1.applied["messages"][-1]
    assert "incorporate it into your current task" in _content(last)
    assert "focus on performance" in _content(last)

    # Next call: nothing new -> still in system prompt, but NOT re-delivered.
    out2 = mw._inject(_Req("BASE", [HumanMessage("hi")]))
    assert "focus on performance" in out2.applied["system_message"].content
    assert "messages" not in out2.applied


def test_existing_instructions_are_not_renudged_after_a_rebuild():
    """An agent rebuild constructs a fresh middleware holding the shared list;
    long-standing rules must not surface again as "just added" messages."""
    instrs = [SteeringInstruction("steer", "focus on performance")]
    mw = SteeringMiddleware(instructions=instrs)

    # Rebuild: the existing rule is seeded as delivered -> no nudge message.
    out = mw._inject(_Req("BASE", [HumanMessage("hi")]))
    assert "focus on performance" in out.applied["system_message"].content
    assert "messages" not in out.applied

    # A runtime-added steer still nudges exactly once.
    instrs.append(SteeringInstruction("steer", "second — added mid-run"))
    out2 = mw._inject(_Req("BASE", [HumanMessage("hi")]))
    assert "messages" in out2.applied
    assert "second — added mid-run" in _content(out2.applied["messages"][-1])
    out3 = mw._inject(_Req("BASE", [HumanMessage("hi")]))
    assert "messages" not in out3.applied  # nudged exactly once


def test_a_standing_steer_reaches_the_subagent_system_prompt():
    """S1: subagent stacks (sync `task` children) mount the SAME shared steering
    list as the main agent, so a standing /steer rule applies inside subagents —
    without re-nudging the existing rule when a subagent graph is built."""
    from novacode_cli.agents.core_agent import _harden_subagent_specs

    shared = [SteeringInstruction("constraint", "never touch tests")]
    spec = {"name": "researcher", "description": "d", "system_prompt": "p", "tools": []}
    compiled = {"name": "remote-one", "url": "http://example.test", "middleware": []}
    hardened = _harden_subagent_specs([spec, compiled], steering_instructions=shared)

    steer_mws = [
        m for m in hardened[0]["middleware"] if type(m).__name__ == "SteeringMiddleware"
    ]
    assert len(steer_mws) == 1, "exactly one steering middleware on the subagent stack"
    assert steer_mws[0]._instructions is shared, "same list instance as the main agent"

    out = steer_mws[0]._inject(_Req("BASE", [HumanMessage("go")]))
    assert "never touch tests" in out.applied["system_message"].content
    assert "messages" not in out.applied, "existing rule: no rebuild nudge"

    # A rule added mid-session (shared list) shows up on the next subagent call.
    shared.append(SteeringInstruction("focus", "second — added mid-run"))
    out2 = steer_mws[0]._inject(_Req("BASE", [HumanMessage("go")]))
    assert "second — added mid-run" in _content(out2.applied["messages"][-1])

    # Compiled/remote subagents (server-built graphs) are left untouched.
    assert "middleware" not in compiled or compiled.get("url"), "remote spec not reconfigured"
    assert hardened[1] is compiled


def test_new_steer_added_later_is_delivered():
    instrs = [SteeringInstruction("steer", "first")]
    mw = SteeringMiddleware(instructions=instrs)
    mw._inject(_Req())  # delivers "first"

    instrs.append(SteeringInstruction("steer", "second — added mid-run"))
    out = mw._inject(_Req("BASE", [HumanMessage("x")]))
    last = _content(out.applied["messages"][-1])
    assert "second — added mid-run" in last
    assert "first" not in last  # already delivered, not repeated as a message
    # both still persist in the system prompt
    assert "first" in out.applied["system_message"].content
    assert "second — added mid-run" in out.applied["system_message"].content


def test_noop_when_no_instructions():
    mw = SteeringMiddleware(instructions=[])
    req = _Req("BASE", [])
    assert mw._inject(req) is req  # unchanged passthrough


def test_disabled_is_passthrough():
    mw = SteeringMiddleware(instructions=[SteeringInstruction("s", "x")], enabled=False)
    req = _Req("BASE", [])
    assert mw._inject(req) is req


class _Model:
    def __init__(self, window):
        self.profile = {"max_input_tokens": window}


class _ReqM(_Req):
    """A _Req that also exposes a model with a known context window."""

    def __init__(self, system_prompt, messages, window):
        super().__init__(system_prompt, messages)
        self.model = _Model(window)


def test_steering_skipped_when_history_overflows_window():
    """A long conversation that alone nears the window must NOT get the steering
    block appended (that overflow truncates the model's answer mid-sentence) —
    but the newly-added steer still reaches the model in-band."""
    big = HumanMessage("x" * 3000)  # ~1000 tokens of history
    mw = SteeringMiddleware(
        instructions=[SteeringInstruction("steer", "focus")], treat_existing_as_new=True
    )
    out = mw._inject(_ReqM("BASE", [big], window=1000))
    assert "system_message" not in out.applied  # block skipped (would overflow)
    assert "messages" in out.applied  # but the nudge still fires
    assert "focus" in _content(out.applied["messages"][-1])


def test_margin_counts_image_parts_by_serialized_length():
    """A base64 image part used to count as zero characters (only its text key
    was read), letting history+steering overflow the window. It must count."""
    image_part = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + "A" * 6000},
    }
    message = HumanMessage(["look at this", image_part])

    mw = SteeringMiddleware(instructions=[SteeringInstruction("s", "x")])
    # ~2000+ serialized chars ≈ 700+ tokens: alone fits a big window, but pushes
    # a small window past the margin once the block is added.
    assert mw._prompt_within_margin(_ReqM("BASE", [message], window=100_000), added_chars=50)
    assert not mw._prompt_within_margin(_ReqM("BASE", [message], window=1000), added_chars=50)


def test_margin_skip_notifies_once_per_streak():
    import novacode_cli.events as events_mod

    mw = SteeringMiddleware(instructions=[SteeringInstruction("steer", "focus")])
    events_mod.nova_event_log.clear()

    # Skips: one notice for the whole streak, not one per call.
    mw._inject(_ReqM("BASE", [HumanMessage("x" * 3000)], window=1000))
    mw._inject(_ReqM("BASE", [HumanMessage("x" * 3000)], window=1000))
    skips = [
        e
        for e in events_mod.nova_event_log.drain("test")
        if e[0] == "steering_skipped"
    ]
    assert len(skips) == 1
    assert "Standing steering held back" in skips[0][3]

    # A successful injection clears the streak, so a later skip notifies again.
    mw._inject(_ReqM("BASE", [HumanMessage("hi")], window=100_000))
    events_mod.nova_event_log.clear()
    mw._inject(_ReqM("BASE", [HumanMessage("x" * 3000)], window=1000))
    skips = [
        e
        for e in events_mod.nova_event_log.drain("test")
        if e[0] == "steering_skipped"
    ]
    assert len(skips) == 1


def test_steering_injected_when_history_fits():
    mw = SteeringMiddleware(instructions=[SteeringInstruction("steer", "focus")])
    out = mw._inject(_ReqM("BASE", [HumanMessage("hi")], window=100_000))
    assert "system_message" in out.applied
    assert "focus" in out.applied["system_message"].content


def test_margin_counts_messages_not_just_system_prompt():
    """Regression: the guard must include the conversation, not only the system
    prompt — a tiny system prompt with a huge history previously slipped through."""
    mw = SteeringMiddleware(instructions=[SteeringInstruction("s", "x")])
    huge_history = [HumanMessage("y" * 5000)]
    assert (
        mw._prompt_within_margin(_ReqM("tiny", huge_history, window=1000), added_chars=50) is False
    )
    assert (
        mw._prompt_within_margin(_ReqM("tiny", [HumanMessage("hi")], window=1000), added_chars=50)
        is True
    )


def test_skip_notice_event_stays_transient():
    """The skip notice rides nova_event_log -> ContextMessage (transient toast);
    it must never join the transcript allowlist in the TUI event handler."""
    import re
    from pathlib import Path

    app_source = (Path(__file__).resolve().parents[1] / "novacode_cli" / "tui" / "app.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r"transcript=e\.event_type\s*in\s*\{([^}]*)\}", app_source)
    assert match, "transcript allowlist moved — re-anchor this test"
    assert "steering_skipped" not in match.group(1)


def test_reset_conversation_preserves_list_identity():
    """The agent middleware holds the same list; reset must mutate it in place."""
    from novacode_cli.states.Session import SessionState

    ss = SessionState()
    shared = ss.steering_instructions  # the object the middleware would hold
    shared.append(SteeringInstruction("steer", "keep me out after clear"))
    ss.reset_conversation()
    assert ss.steering_instructions is shared  # same object (not reassigned)
    assert ss.steering_instructions == []  # cleared in place
