"""Compaction keeps the recent tail verbatim, and the summarizer sees tool calls.

Measured against how Codex CLI (keeps ~20k tokens of recent messages),
deepagents' own SummarizationMiddleware (keeps 10% of the window) and Claude
Code (re-attaches recent files) compact. Each test fails on the previous code,
which replaced the WHOLE history with one summary written from message
``.content`` alone.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import novacode_cli.compaction as C


def _turn(n: int, size: int = 100) -> list:
    """One user turn: ask → tool call → result → answer."""
    return [
        HumanMessage(content=f"ask {n}", id=f"h{n}"),
        AIMessage(
            content="",
            id=f"a{n}",
            tool_calls=[{"id": f"c{n}", "name": "edit_file", "args": {"file_path": f"/src/f{n}.py"}}],
        ),
        ToolMessage(content="x" * size, tool_call_id=f"c{n}", name="edit_file", id=f"t{n}"),
        AIMessage(content=f"done {n}", id=f"d{n}"),
    ]


# ── the summarizer sees what the agent did ─────────────────────────────────


def test_tool_calls_reach_the_summarizer() -> None:
    parts = "\n".join(C._message_parts(_turn(1)))
    assert "edit_file" in parts and "/src/f1.py" in parts, parts


def test_huge_tool_args_are_capped() -> None:
    msg = AIMessage(
        content="",
        tool_calls=[{"id": "c", "name": "write_file", "args": {"content": "y" * 50_000}}],
    )
    assert len(C._message_parts([msg])[0]) < 500


# ── where the verbatim tail starts ─────────────────────────────────────────


def test_tail_starts_at_a_user_message() -> None:
    msgs = _turn(1) + _turn(2) + _turn(3)
    cut = C._split_tail(msgs, keep_tokens=40)  # ~160 chars: one turn fits
    assert isinstance(msgs[cut], HumanMessage)
    assert msgs[cut:] == _turn(3)


def test_tail_takes_as_many_whole_turns_as_fit() -> None:
    msgs = _turn(1) + _turn(2) + _turn(3)
    assert C._split_tail(msgs, keep_tokens=10_000) == 4  # everything but turn 1


def test_last_turn_too_big_keeps_nothing() -> None:
    msgs = _turn(1) + _turn(2, size=50_000)
    assert C._split_tail(msgs, keep_tokens=100) == len(msgs)


def test_tail_never_orphans_a_tool_result() -> None:
    """A steer injected between a call and its result is also a HumanMessage."""
    msgs = [
        HumanMessage(content="go", id="h0"),
        AIMessage(content="", id="a0", tool_calls=[{"id": "c0", "name": "ls", "args": {}}]),
        HumanMessage(content="steer: also check tests", id="s0"),
        ToolMessage(content="ok", tool_call_id="c0", name="ls", id="t0"),
    ]
    cut = C._split_tail(msgs, keep_tokens=10_000)
    assert cut == len(msgs), "tail would start with a result whose call was summarized"


# ── compact_conversation end to end ─────────────────────────────────────────


class _Agent:
    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.updated: dict | None = None

    async def aget_state(self, config):  # noqa: ANN001, ANN201, ARG002
        return type("S", (), {"values": {"messages": self.messages}})()

    async def aupdate_state(self, *, config, values, as_node=None):  # noqa: ANN001, ANN201, ARG002
        self.updated = values


class _Model:
    model_name = "mock-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ainvoke(self, msgs):  # noqa: ANN001, ANN201
        self.prompts.append(msgs[0].content)
        return AIMessage(content="SUMMARY")


def test_compaction_keeps_the_last_turn_verbatim_after_the_summary(monkeypatch) -> None:
    monkeypatch.setattr(C, "_rehydration_note", lambda: "")
    history = _turn(1) + _turn(2) + _turn(3)
    agent, model = _Agent(history), _Model()

    result = asyncio.run(C.compact_conversation(agent, model, "t", context_window=4_000))

    assert result.success, result.error
    new = agent.updated["messages"]
    assert isinstance(new[0], RemoveMessage) and new[0].id == REMOVE_ALL_MESSAGES
    assert C.is_compaction_summary(new[1].content), "summary must come BEFORE the tail"
    assert new[2:] == history[4:], "the recent turns must survive verbatim"
    assert result.messages_after == 1 + len(history[4:])
    # The summarizer was only given what the tail does not already carry.
    assert "ask 1" in model.prompts[0] and "ask 3" not in model.prompts[0]
    assert "kept VERBATIM" in model.prompts[0]


def test_summary_tells_the_model_to_continue_and_where_the_files_are(monkeypatch) -> None:
    import novacode_cli.tracking.file_tracker as ft

    monkeypatch.setattr(ft, "get_modified_files", lambda: ["/src/app.py"])
    monkeypatch.setattr(ft, "get_recently_read_files", lambda limit=10: ["/src/app.py", "/README.md"])
    agent = _Agent(_turn(1))

    asyncio.run(C.compact_conversation(agent, _Model(), "t", context_window=4_000))

    summary = agent.updated["messages"][1].content
    assert "Continue the work" in summary and "Do not acknowledge" in summary
    assert "Modified: /src/app.py" in summary
    assert "Read recently: /README.md" in summary, "a modified file is not listed twice"


def test_template_asks_for_one_format_only() -> None:
    """It used to describe nine sections, then demand a different six."""
    text = C.render_template("summarization.jinja", focus_instructions="", conversation="")
    assert "### Files Modified" not in text and "### Work Completed" not in text
    assert "kept VERBATIM" not in text, "the tail note is only for a kept tail"


# ── tool-result clearing ────────────────────────────────────────────────────


def test_old_file_reads_are_cleared_with_a_recovery_hint() -> None:
    from novacode_cli.agents.core_agent import CLEARED_TOOL_RESULT, _tool_result_clearing

    msgs: list = [HumanMessage(content="go")]
    for i in range(8):
        msgs.append(
            AIMessage(content="", tool_calls=[{"id": f"r{i}", "name": "read_file", "args": {}}])
        )
        msgs.append(ToolMessage(content="z" * 4_000, tool_call_id=f"r{i}", name="read_file"))

    _tool_result_clearing(0).apply(msgs, count_tokens=lambda m: 10**9)

    cleared = [m for m in msgs if isinstance(m, ToolMessage) and m.content == CLEARED_TOOL_RESULT]
    assert len(cleared) == 3, "the 3 oldest reads should go, the last 5 stay"
    assert "Re-run" in CLEARED_TOOL_RESULT
