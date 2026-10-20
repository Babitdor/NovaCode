"""Snip and collapse: the two strategies that run before an automatic compaction."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.context_compaction import ContextCompactionMiddleware
from novacode_cli.context.strategies import (
    COLLAPSE_AT,
    collapse_boundary,
    collapse_turns,
    snip_history,
)


def _read(n: int, path: str, text: str) -> list:
    call = {"id": f"c{n}", "name": "read_file", "args": {"file_path": path}, "type": "tool_call"}
    return [
        AIMessage(content="", id=f"a{n}", tool_calls=[call]),
        ToolMessage(text, tool_call_id=f"c{n}", name="read_file", id=f"t{n}"),
    ]


def _turn(n: int, result: str) -> list:
    return [
        HumanMessage(content=f"task {n}", id=f"h{n}"),
        *_read(n, f"/src/f{n}.py", result),
        AIMessage(content=f"answer {n}", id=f"ans{n}"),
    ]


def test_snip_drops_zombies_and_stale_reads_before_the_current_turn():
    note = "Internal context: skills that may apply.\n- x"
    history = [
        HumanMessage(content="first", id="u1"),
        HumanMessage(content=note, id="n1"),
        *_read(1, "/app.py", "old app " * 300),
        AIMessage(content="", id="empty"),
        AIMessage(content="done", id="d1"),
        HumanMessage(content="second", id="u2"),
        HumanMessage(content=note, id="n2"),
        *_read(2, "/app.py", "new app " * 300),
        AIMessage(content="done again", id="d2"),
        HumanMessage(content="third", id="u3"),  # the current turn starts here
        *_read(3, "/app.py", "newest app " * 300),
    ]
    view, report = snip_history(history)
    ids = [m.id for m in view]
    assert "empty" not in ids
    assert "n1" not in ids  # an older copy of the same note
    assert "n2" in ids
    t1 = next(m for m in view if m.id == "t1")
    assert t1.content.startswith("[Superseded read of /app.py")
    # The current turn is never edited, even though t3 supersedes t2.
    assert next(m for m in view if m.id == "t2").content.startswith("[Superseded")
    assert next(m for m in view if m.id == "t3").content.startswith("newest app")
    assert report.kinds == {
        "empty assistant message": 1,
        "repeated internal note": 1,
        "superseded file read": 2,
    }
    assert report.chars > 4_000


def test_snip_keeps_every_tool_result_paired_with_its_call():
    history = [
        HumanMessage(content="go", id="u"),
        *_read(1, "/a", "a" * 900),
        *_read(2, "/a", "b" * 900),
    ]
    history.append(HumanMessage(content="next", id="u2"))
    view, _ = snip_history(history)
    issued = {c["id"] for m in view if isinstance(m, AIMessage) for c in m.tool_calls}
    assert all(m.tool_call_id in issued for m in view if isinstance(m, ToolMessage))


def test_collapse_folds_old_turns_into_a_digest_and_keeps_answers():
    history = [m for n in range(1, 6) for m in _turn(n, "line\n" * 300)]
    history.append(HumanMessage(content="now this", id="current"))
    boundary = collapse_boundary(history)
    assert boundary == "h4"  # turns 4 and 5 plus the current one stay verbatim
    saved: list[tuple] = []

    def save(call_id: str, name: str | None, text: str) -> str:
        saved.append((call_id, name, len(text)))
        return f"/cleared/{call_id}.txt"

    view, report = collapse_turns(history, boundary, save)
    assert report.turns == 3
    assert [m.id for m in view[:2]] == ["h1", "ans1"]
    digest = view[1].content
    assert digest.startswith("[Collapsed earlier work: 1 tool calls.")
    assert "read_file /src/f1.py → 300 lines (saved: /cleared/c1.txt)" in digest
    assert digest.endswith("answer 1")
    assert not any(isinstance(m, ToolMessage) and m.id in {"t1", "t2", "t3"} for m in view)
    assert [m.id for m in view[-9:]] == [
        "h4",
        "a4",
        "t4",
        "ans4",
        "h5",
        "a5",
        "t5",
        "ans5",
        "current",
    ]
    assert [c for c, _, _ in saved] == ["c1", "c2", "c3"]


def test_collapse_without_a_boundary_changes_nothing():
    history = [m for n in range(1, 6) for m in _turn(n, "x" * 500)]
    assert collapse_turns(history, None)[0] == history
    assert collapse_turns(history, "summarized-away")[0] == history


class _Request(SimpleNamespace):
    def override(self, **changes: Any) -> _Request:
        return _Request(**{**vars(self), **changes})


def _big_history(turns: int, chars: int) -> list:
    history = [m for n in range(1, turns + 1) for m in _turn(n, "y" * chars)]
    return [*history, HumanMessage(content="next task", id="current")]


def test_middleware_collapses_at_the_start_of_a_turn_and_saves_the_boundary():
    window = 100_000
    middleware = ContextCompactionMiddleware(context_window=window)
    # ~65k tokens: past the collapse threshold, below the summary trigger.
    history = _big_history(5, 52_000)
    request = _Request(model=None, messages=history, system_message=None, tools=[], state={})
    sent: list = []

    async def handler(req: Any) -> Any:
        sent.append(req.messages)
        return SimpleNamespace(result=[AIMessage(content="ok")])

    response = asyncio.run(middleware.awrap_model_call(request, handler))
    assert response.command.update["_collapse_event"] == {"boundary_id": "h4"}
    view = sent[0]
    assert not any(m.id == "t1" for m in view), "an old turn's result was still sent"
    assert any(m.id == "t5" for m in view)
    assert middleware.limits(None).trigger > int(window * COLLAPSE_AT)

    # Mid-turn (an assistant reply after the user message), the boundary stays put.
    mid = [
        *history,
        AIMessage(content="", id="r", tool_calls=[{"id": "q", "name": "read_file", "args": {}}]),
    ]
    mid.append(ToolMessage("z", tool_call_id="q", name="read_file", id="tq"))
    state = {"_collapse_event": {"boundary_id": "h4"}}
    plan = middleware._plan(
        _Request(model=None, messages=mid, system_message=None, tools=[], state=state)
    )
    assert plan.collapse_boundary == "h4"
    assert plan.collapse_update is None


def test_small_conversations_are_never_collapsed():
    middleware = ContextCompactionMiddleware(context_window=100_000)
    history = _big_history(5, 500)
    plan = middleware._plan(
        _Request(model=None, messages=history, system_message=None, tools=[], state={})
    )
    assert plan.collapse_boundary is None
    assert plan.collapse_update is None


# ── /context accounting: the MCP share and the system prompt, measured ─────


def test_snapshot_measures_the_system_prompt_and_the_mcp_share():
    from langchain_core.messages import SystemMessage
    from langchain_core.tools import tool

    from novacode_cli.context.snapshot import ContextSnapshot

    @tool
    def read_file(path: str) -> str:
        """Read a file."""
        return path

    @tool
    def serena_find_symbol(name: str, depth: int = 0) -> str:
        """Find a symbol in the project and return its definition and references."""
        return name

    serena_find_symbol.metadata = {"nova_mcp_server": "serena"}
    system = SystemMessage(
        "You are Nova.\n\n## MCP Tools\n\nAvailable MCP servers:\n"
        + "**serena** (stdio)\n  Tools (28): "
        + ", ".join(f"tool_{i}" for i in range(28))
        + "\n\n## Skills\n\nmore prompt"
    )
    request = _Request(
        model=None,
        messages=[HumanMessage("hi")],
        system_message=system,
        tools=[read_file, serena_find_symbol],
        state={},
    )
    diag = ContextSnapshot.capture(request).diagnostics()
    assert diag["mcp_tools"] == 1
    assert 0 < diag["mcp_schema_tokens"] < diag["estimated_schema_tokens"]
    assert 0 < diag["mcp_prompt_tokens"] < diag["estimated_system_tokens"]


def test_breakdown_anchors_system_and_tools_to_the_request():
    from novacode_cli.context._analysis import ContextBreakdown

    # The startup baseline missed the per-request prompt (MCP list, memory):
    # 400 here against 3,000 actually sent.
    estimate = ContextBreakdown(
        system_prompt_tokens=400,
        tool_definitions_tokens=900,
        user_message_tokens=1_000,
        assistant_message_tokens=500,
        tool_result_tokens=500,
        total_tokens=3_300,
    )
    snapshot = {
        "estimated_input_tokens": 10_000,
        "estimated_system_tokens": 3_000,
        "estimated_schema_tokens": 1_000,
        "mcp_tools": 2,
        "mcp_schema_tokens": 300,
        "mcp_prompt_tokens": 450,
    }
    anchored = estimate.anchored_to(snapshot)
    assert anchored.system_prompt_tokens == 3_000
    assert anchored.tool_definitions_tokens == 1_000
    assert anchored.conversation_tokens == 6_000  # the rest, in the same proportions
    assert anchored.user_message_tokens == 3_000
    assert anchored.total_tokens == 10_000
    assert (anchored.mcp_tool_count, anchored.mcp_tool_tokens, anchored.mcp_prompt_tokens) == (
        2,
        300,
        450,
    )
    # A snapshot from before this field existed still scales as before.
    old = estimate.anchored_to({"estimated_input_tokens": 6_600})
    assert old.total_tokens == 6_600
    assert old.system_prompt_tokens == 800


def test_mcp_tools_carry_their_server_for_accounting(tmp_path):
    import json

    from mcp.types import Tool

    from novacode_cli.mcp.middleware import MCPMiddleware

    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"serena": {"transport": "http", "url": "https://x.test/mcp"}}}),
        encoding="utf-8",
    )
    tools = MCPMiddleware(config)._convert_raw_tools(
        "serena",
        {"transport": "streamable_http", "url": "https://x.test/mcp"},
        [Tool(name="find_symbol", inputSchema={"type": "object"})],
    )
    assert tools[0].metadata["nova_mcp_server"] == "serena"


def test_context_counts_tool_schemas_as_sent_not_as_registered():
    """The task tool's full subagent roster is trimmed before it is sent."""
    from langchain_core.tools import StructuredTool

    from novacode_cli.agents.tool_search import sent_tools
    from novacode_cli.context._analysis import build_context_breakdown

    roster = "\n".join(f"- agent-{i}: does thing {i} " + "x" * 300 for i in range(60))
    task = StructuredTool.from_function(
        lambda description, subagent_type: "",
        name="task",
        description="Launch a subagent.\nAvailable agents:\n- general-purpose: anything\n" + roster,
    )
    search = StructuredTool.from_function(
        lambda query: "", name="search_tools", description="Find tools."
    )
    extra = StructuredTool.from_function(lambda x: "", name="rarely_used", description="y" * 4_000)
    sent = sent_tools([task, search, extra], [HumanMessage("hi")])
    assert "rarely_used" not in {t.name for t in sent}
    trimmed = next(t for t in sent if t.name == "task").description
    assert "general-purpose" in trimmed
    assert "agent-7" not in trimmed
    counted = build_context_breakdown([], "gpt-4o", tools=[task, search, extra])
    assert counted.tool_definitions_tokens < 1_000


def test_remembered_system_prompt_size_round_trips(monkeypatch, tmp_path):
    from novacode_cli.config import config
    from novacode_cli.context.snapshot import remember_system_tokens, remembered_system_tokens

    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    assert remembered_system_tokens("agent") == 0
    remember_system_tokens("agent", 18_100)
    remember_system_tokens("other", 9_000)
    assert remembered_system_tokens("agent") == 18_100
    assert remembered_system_tokens("other") == 9_000
