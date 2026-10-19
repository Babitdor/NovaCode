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
    mw = SteeringMiddleware(instructions=instrs)

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
    mw = SteeringMiddleware(instructions=[SteeringInstruction("steer", "focus")])
    out = mw._inject(_ReqM("BASE", [big], window=1000))
    assert "system_message" not in out.applied  # block skipped (would overflow)
    assert "messages" in out.applied  # but the nudge still fires
    assert "focus" in _content(out.applied["messages"][-1])


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


def test_reset_conversation_preserves_list_identity():
    """The agent middleware holds the same list; reset must mutate it in place."""
    from novacode_cli.states.Session import SessionState

    ss = SessionState()
    shared = ss.steering_instructions  # the object the middleware would hold
    shared.append(SteeringInstruction("steer", "keep me out after clear"))
    ss.reset_conversation()
    assert ss.steering_instructions is shared  # same object (not reassigned)
    assert ss.steering_instructions == []  # cleared in place
