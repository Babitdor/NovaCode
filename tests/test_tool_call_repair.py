"""Tool calls in history always get their results, adjacent to the call.

OpenAI-strict endpoints reject a history where an assistant message's
``tool_calls`` are not immediately followed by a tool message for each:

    400 invalid_request_error — An assistant message with 'tool_calls' must be
    followed by tool messages responding to each 'tool_call_id'.

Found in the user's own saved history: of 514 conversations, 5 broke the rule,
and 4 of the 5 were followed by exactly "[The previous request was cancelled by
the system]" — the note agent_loop writes when a turn is cancelled while a tool
runs. Lenient providers accept the malformed history silently, which is why it
surfaced only on some models.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.tool_call_repair import RepairToolCallsEachStep
from novacode_cli.core.agent_loop import _CANCEL_NOTICE, _record_cancellation


def _call(cid: str, name: str = "ls") -> dict:
    return {"name": name, "args": {}, "id": cid, "type": "tool_call"}


def _strict(messages) -> list[str]:
    """OpenAI's rule, exactly: every call answered by the IMMEDIATELY
    following tool messages. Returns the ids that violate it."""
    bad: list[str] = []
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and m.tool_calls:
            got = set()
            for n in messages[i + 1 :]:
                if n.type != "tool":
                    break
                got.add(n.tool_call_id)
            bad.extend(tc["id"] for tc in m.tool_calls if tc["id"] not in got)
    return bad


# ── The cancel path, fixed at its source ────────────────────────────────────


class _FakeAgent:
    """Holds a message list the way a checkpointer would."""

    def __init__(self, messages: list) -> None:
        self.messages = list(messages)

    async def aget_state(self, config):
        class _Snap:
            values = {"messages": self.messages}

        return _Snap()

    async def aupdate_state(self, config=None, values=None, as_node=None):
        self.messages.extend(values["messages"])


def _cancel(history: list) -> list:
    agent = _FakeAgent(history)
    asyncio.run(_record_cancellation(agent, {}))
    return agent.messages


def test_cancelling_mid_tool_leaves_a_valid_history():
    """The exact shape found in real saved conversations."""
    out = _cancel([HumanMessage("go"), AIMessage(content="", tool_calls=[_call("c1")])])
    assert _strict(out) == [], f"left an unanswered call: {_strict(out)}"
    assert out[-1].content == _CANCEL_NOTICE


def test_every_parallel_call_is_answered_and_adjacent():
    calls = [_call("p1"), _call("p2"), _call("p3")]
    out = _cancel([HumanMessage("go"), AIMessage(content="", tool_calls=calls)])
    assert _strict(out) == []
    assert [m.type for m in out[-4:]] == ["tool", "tool", "tool", "human"]


def test_the_cancelled_results_say_what_happened():
    out = _cancel([HumanMessage("go"), AIMessage(content="", tool_calls=[_call("c1", "shell")])])
    result = next(m for m in out if isinstance(m, ToolMessage))
    assert result.tool_call_id == "c1"
    assert "cancel" in result.content.lower()


def test_only_unanswered_calls_get_a_synthetic_result():
    """A call whose tool already reported must not be answered twice."""
    history = [
        HumanMessage("go"),
        AIMessage(content="", tool_calls=[_call("done"), _call("pending")]),
        ToolMessage(content="real result", tool_call_id="done"),
    ]
    # Not the cancel-mid-tool shape (last message is a tool result), so the
    # helper writes only the notice and leaves repair to the middleware.
    out = _cancel(history)
    assert sum(1 for m in out if getattr(m, "tool_call_id", None) == "done") == 1


def test_a_cancel_with_no_tool_in_flight_just_writes_the_notice():
    out = _cancel([HumanMessage("hi"), AIMessage("thinking...")])
    assert [m.type for m in out] == ["human", "ai", "human"]
    assert out[-1].content == _CANCEL_NOTICE


def test_recording_a_cancel_survives_an_unreadable_state():
    """Writing the notice must never depend on reading history successfully."""

    class _Broken(_FakeAgent):
        async def aget_state(self, config):
            msg = "checkpointer unavailable"
            raise RuntimeError(msg)

    agent = _Broken([])
    asyncio.run(_record_cancellation(agent, {}))
    assert [m.content for m in agent.messages] == [_CANCEL_NOTICE]


# ── The repair, now before every model call ─────────────────────────────────


def _patched(result):
    msgs = result["messages"]
    msgs = list(getattr(msgs, "value", msgs))  # Overwrite (older) or a plain list
    # deepagents 0.6.11 and 0.7.x both lead with a REMOVE_ALL marker.
    return [m for m in msgs if m.type != "remove"]


def test_repair_runs_before_each_model_call():
    """deepagents' own patcher runs once per invocation; mid-turn damage
    reached the next model call as-is."""
    state = {
        "messages": [
            HumanMessage("go"),
            AIMessage(content="", tool_calls=[_call("x")]),
            HumanMessage("steer"),
        ]
    }
    out = _patched(RepairToolCallsEachStep().before_model(state, None))
    assert _strict(out) == []
    # The result lands right after its call — before the steer, not at the end.
    assert [m.type for m in out] == ["human", "ai", "tool", "human"]


def test_async_repair_matches_sync():
    state = {"messages": [HumanMessage("go"), AIMessage(content="", tool_calls=[_call("x")])]}
    out = asyncio.run(RepairToolCallsEachStep().abefore_model(state, None))
    assert _strict(_patched(out)) == []


def test_a_valid_history_is_left_alone():
    """Returning a rewrite on every model call would churn persisted state."""
    state = {
        "messages": [
            HumanMessage("go"),
            AIMessage(content="", tool_calls=[_call("x")]),
            ToolMessage(content="r", tool_call_id="x"),
        ]
    }
    assert RepairToolCallsEachStep().before_model(state, None) is None


def test_repair_does_not_collide_with_the_deepagents_patcher():
    """Both are installed; LangChain rejects two middleware with one name."""
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware

    assert RepairToolCallsEachStep().name != PatchToolCallsMiddleware().name


def test_the_main_agent_installs_the_repair():
    """Read as text rather than imported: core_agent pulls the full agent
    stack, which this check does not need and some envs cannot import."""
    from pathlib import Path

    import novacode_cli

    source = (
        Path(novacode_cli.__file__).parent / "agents" / "core_agent.py"
    ).read_text(encoding="utf-8")
    assert "agent_middleware.append(RepairToolCallsEachStep(" in source, (
        "the main agent is built without per-call tool-call repair"
    )


# ── Cross-provider sanitation: the shape a provider switch produces ─────────


def _sanitize(messages, model=None):
    from novacode_cli.agents.tool_call_repair import sanitize_history

    return sanitize_history(messages, model)


def test_an_orphaned_tool_result_is_dropped():
    """The OpenCode Go 400: a tool message with no preceding tool_calls."""
    history = [
        HumanMessage("go"),
        ToolMessage(content="stale result", tool_call_id="gone"),
        AIMessage("done"),
    ]
    out = _sanitize(history)
    assert [m.type for m in out] == ["human", "ai"], "orphaned tool result survived"


def test_a_paired_result_is_kept():
    history = [
        AIMessage(content="", tool_calls=[_call("c1")]),
        ToolMessage(content="ok", tool_call_id="c1"),
    ]
    assert _sanitize(history) is history, "a valid sequence was rewritten"


def test_raw_tool_calls_are_promoted_so_results_stay_paired():
    """A provider that stored calls only in additional_kwargs left the result
    orphaned for the next converter, which reads the typed field."""
    # ``model_construct`` bypasses pydantic's auto-parse: AIMessage normally
    # mirrors additional_kwargs["tool_calls"] into .tool_calls, but a message
    # rehydrated from a non-standard provider can carry only the raw field.
    assistant = AIMessage.model_construct(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "ls", "arguments": "{}"},
                }
            ]
        },
        tool_calls=[],
    )
    assert not assistant.tool_calls
    history = [assistant, ToolMessage(content="a.py", tool_call_id="c1")]
    out = _sanitize(history)
    assert [m.type for m in out] == ["ai", "tool"], "promotion lost the pairing"
    assert out[0].tool_calls[0]["id"] == "c1"


def test_reasoning_from_another_model_is_stripped():
    """After a switch the new provider never asked for the old chain of thought."""
    old = AIMessage(
        content="hi",
        additional_kwargs={"reasoning_content": "old thoughts"},
        response_metadata={"model_name": "old-model"},
    )
    out = _sanitize([old], "new-model")
    assert "reasoning_content" not in out[0].additional_kwargs


def test_reasoning_from_the_same_model_is_kept():
    """A thinking model that stays in use needs its own reasoning echoed back."""
    same = AIMessage(
        content="hi",
        additional_kwargs={"reasoning_content": "keep me"},
        response_metadata={"model_name": "same-model"},
    )
    out = _sanitize([same], "same-model")
    assert out is same or out[0].additional_kwargs.get("reasoning_content") == "keep me"


def test_foreign_thinking_content_blocks_are_stripped():
    """Anthropic's signed thinking blocks are not portable to another provider."""
    msg = AIMessage(
        content=[
            {"type": "thinking", "thinking": "secret", "signature": "sig"},
            {"type": "text", "text": "answer"},
        ],
        response_metadata={"model_name": "claude-opus"},
    )
    out = _sanitize([msg], "gpt-5-mini")
    assert out[0].content == [{"type": "text", "text": "answer"}]


def test_same_model_thinking_blocks_survive():
    msg = AIMessage(
        content=[{"type": "thinking", "thinking": "keep", "signature": "sig"}],
        response_metadata={"model_name": "claude-opus"},
    )
    out = _sanitize([msg], "claude-opus")
    assert out is msg or out[0].content[0].get("type") == "thinking"


def test_a_real_switch_no_longer_sends_the_orphan():
    """End to end through the middleware the agent installs."""
    from novacode_cli.agents.tool_call_repair import RepairToolCallsEachStep

    state = {
        "messages": [
            HumanMessage("go"),
            ToolMessage(content="orphan", tool_call_id="nope"),
            AIMessage("done"),
        ]
    }
    out = _patched(RepairToolCallsEachStep().before_model(state, None))
    assert [m.type for m in out] == ["human", "ai"]

