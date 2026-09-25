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


def _read_history(n: int = 8, tool: str = "read_file") -> list:
    msgs: list = [HumanMessage(content="go")]
    for i in range(n):
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"r{i}", "name": tool, "args": {}}]))
        msgs.append(ToolMessage(content=f"payload-{i}" * 400, tool_call_id=f"r{i}", name=tool))
    return msgs


def test_old_file_reads_are_cleared_with_a_recovery_hint() -> None:
    from novacode_cli.agents.core_agent import CLEARED_TOOL_RESULT, _tool_result_clearing

    msgs = _read_history()
    _tool_result_clearing(0).apply(msgs, count_tokens=lambda m: 10**9)

    cleared = [m for m in msgs if isinstance(m, ToolMessage) and m.content == CLEARED_TOOL_RESULT]
    assert len(cleared) == 3, "the 3 oldest reads should go, the last 5 stay"
    assert "Re-run" in CLEARED_TOOL_RESULT, "no offload dir: say what was lost"


def test_cleared_payloads_are_offloaded_not_destroyed(tmp_path) -> None:
    """Restorable compaction: the bytes leave the context, not the machine."""
    from novacode_cli.agents.core_agent import _tool_result_clearing

    msgs = _read_history()
    _tool_result_clearing(0, tmp_path).apply(msgs, count_tokens=lambda m: 10**9)

    cleared = [m for m in msgs if isinstance(m, ToolMessage) and "moved out of context" in m.content]
    assert len(cleared) == 3
    for i, msg in enumerate(cleared):
        assert "/cleared/" in msg.content, "the placeholder must name a readable path"
        name = msg.content.split("/cleared/")[1].split(" ")[0]
        assert (tmp_path / name).read_text(encoding="utf-8") == f"payload-{i}" * 400
    assert len(msgs[2].content) < 200, "the context copy is what shrinks"


def test_web_search_results_are_offloaded_too() -> None:
    """They were excluded only because a search cannot be re-run; now it needn't be."""
    from novacode_cli.agents.core_agent import _tool_result_clearing

    assert "web_search" not in _tool_result_clearing(0).exclude_tools
    assert "think" in _tool_result_clearing(0).exclude_tools, "reasoning is not an observation"


def test_offload_failure_falls_back_to_the_honest_placeholder(tmp_path) -> None:
    from novacode_cli.agents.core_agent import _tool_result_clearing

    blocked = tmp_path / "file-in-the-way"
    blocked.write_text("not a directory", encoding="utf-8")
    msgs = _read_history()
    _tool_result_clearing(0, blocked).apply(msgs, count_tokens=lambda m: 10**9)

    from novacode_cli.agents.tool_offload import UNSAVED

    cleared = [m for m in msgs if isinstance(m, ToolMessage) and m.content == UNSAVED]
    assert len(cleared) == 3, "still cleared — the context pressure is real either way"
    assert "[cleared]" not in {m.content for m in msgs}, "the bare marker must never ship"


def test_compaction_archives_the_raw_transcript(monkeypatch, tmp_path) -> None:
    """The summary is a paraphrase; the originals must survive somewhere."""
    from novacode_cli.config import config as cfg

    monkeypatch.setattr(cfg, "HOME_DIR", tmp_path)
    agent = _Agent(_turn(1))

    asyncio.run(C.compact_conversation(agent, _Model(), "thread-x", context_window=4_000))

    dumps = list((tmp_path / "sessions" / "thread-x").glob("pre-compact-*.jsonl"))
    assert len(dumps) == 1
    lines = dumps[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(_turn(1))
    assert str(dumps[0]) in agent.updated["messages"][1].content, "the summary must point at it"


def test_offloaded_results_are_readable_through_the_cleared_route(tmp_path) -> None:
    """The placeholder names /cleared/<file>; the backend must serve it."""
    from novacode_cli.agents.core_agent import _build_composite_backend
    from novacode_cli.agents.tool_offload import cleared_dir

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (tmp_path / "ws").mkdir()
    (tmp_path / "skills").mkdir()
    composite, _ = _build_composite_backend(
        sandbox=None,
        sandbox_type=None,
        workspace_root=tmp_path / "ws",
        skills_dir=tmp_path / "skills",
        claude_skills_dir=tmp_path / "absent",
        project_skills_dirs=[],
        plugin_skills=[],
        agent_dir=agent_dir,
        store=None,
        assistant_id="a",
    )
    cleared_dir(agent_dir).mkdir(parents=True, exist_ok=True)
    (cleared_dir(agent_dir) / "read_file-r0.txt").write_text("the original bytes", "utf-8")

    result = composite.read("/cleared/read_file-r0.txt")
    assert result.error is None
    assert result.file_data["content"] == "the original bytes"
