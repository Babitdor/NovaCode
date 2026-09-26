"""Deferred tools: only core / frequent / loaded schemas reach the model."""

from __future__ import annotations

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

import novacode_cli.skills.retrieval as R
from novacode_cli.agents.tool_search import (
    CORE_TOOLS,
    LOADED_MARKER,
    ToolSearchMiddleware,
    loaded_names,
)


@tool
def read_file(path: str) -> str:
    """Read a file."""
    return path


@tool
def task(description: str) -> str:
    """Launch a subagent.

    Available agent types:
    - reviewer: Reviews code.
    - rust-reviewer: Expert Rust code reviewer for ownership and lifetimes.
    """
    return description


def _mcp(name: str, doc: str):
    @tool(name)
    def _t(x: str) -> str:
        """placeholder"""
        return x

    _t.description = doc
    return _t


BROWSER = [
    _mcp("playwright_browser_navigate", "Navigate the browser to a URL."),
    _mcp("playwright_browser_click", "Click an element on the page."),
    _mcp("playwright_browser_snapshot", "Capture an accessibility snapshot of the page."),
]
TOOLS = [read_file, task, *BROWSER]


@pytest.fixture(autouse=True)
def _bm25_only(monkeypatch):
    monkeypatch.setattr(R, "_model", lambda: None)
    R._index_cache.clear()


def _request(messages: list) -> ModelRequest:
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=messages,
        system_message=SystemMessage(content="base"),
        tools=list(TOOLS),
        state={"messages": messages},
        runtime=None,  # type: ignore[arg-type]
    )


def _mw() -> ToolSearchMiddleware:
    mw = ToolSearchMiddleware(
        deferred_subagents={
            "rust-reviewer": "Expert Rust code reviewer for ownership and lifetimes."
        }
    )
    mw._frequent = set()
    return mw


def _names(req: ModelRequest) -> set[str]:
    return {t.name for t in req.tools}


def test_only_core_tools_are_bound_until_loaded() -> None:
    req = _mw()._apply(_request([HumanMessage("hi")]))
    assert _names(req) == {"read_file", "task"}
    note = req.system_message.content_blocks[-1]["text"]
    assert "`playwright_*`: browser_click, browser_navigate, browser_snapshot" in note


def test_frequent_tools_stay_bound() -> None:
    mw = _mw()
    mw._frequent = {"playwright_browser_click"}
    assert "playwright_browser_click" in _names(mw._apply(_request([HumanMessage("hi")])))


def test_hidden_subagents_leave_the_task_description() -> None:
    req = _mw()._apply(_request([HumanMessage("hi")]))
    desc = next(t for t in req.tools if t.name == "task").description
    assert "rust-reviewer" not in desc and "- reviewer: Reviews code." in desc
    assert "1 more specialist subagents" in desc
    assert "rust-reviewer" in task.description, "the shared tool itself is untouched"


def test_artifact_tools_are_core_not_deferred() -> None:
    """Artifact tools must be bound without a search_tools round-trip.

    The prompt tells the agent to create artifacts proactively; if these drop
    out of CORE_TOOLS the instruction names tools the model cannot see.
    """
    assert {"create_artifact", "update_artifact", "list_artifacts"} <= CORE_TOOLS

    # End-to-end: a request carrying the artifact tools keeps them bound.
    arts = [_mcp(n, "d") for n in ("create_artifact", "update_artifact", "list_artifacts")]
    req = _request([HumanMessage("hi")])
    req = req.override(tools=[*TOOLS, *arts])
    bound = _names(_mw()._apply(req))
    assert {"create_artifact", "update_artifact", "list_artifacts"} <= bound


def test_search_tools_loads_by_description_and_by_exact_name() -> None:
    mw = _mw()
    mw._apply(_request([HumanMessage("hi")]))  # builds the catalog
    by_meaning = mw.tools[0].invoke({"query": "open a url in the browser"})
    assert by_meaning.startswith(LOADED_MARKER) and "playwright_browser_navigate" in by_meaning
    exact = mw.tools[0].invoke({"query": "playwright_browser_click, playwright_browser_snapshot"})
    assert exact.splitlines()[0] == (
        f"{LOADED_MARKER} playwright_browser_click, playwright_browser_snapshot"
    )
    agent = mw.tools[0].invoke({"query": "review rust ownership lifetimes"})
    assert 'task(subagent_type="rust-reviewer")' in agent


def test_loaded_tools_are_bound_from_then_on() -> None:
    mw = _mw()
    mw._apply(_request([HumanMessage("hi")]))
    result = mw.tools[0].invoke({"query": "playwright_browser_click"})
    history = [
        HumanMessage("click the button"),
        AIMessage("", tool_calls=[{"id": "1", "name": "search_tools", "args": {}}]),
        ToolMessage(result, tool_call_id="1", name="search_tools"),
    ]
    assert "playwright_browser_click" in _names(mw._apply(_request(history)))


def test_a_tool_called_without_loading_stays_bound() -> None:
    history = [
        AIMessage("", tool_calls=[{"id": "1", "name": "playwright_browser_navigate", "args": {}}])
    ]
    assert "playwright_browser_navigate" in loaded_names(history)


def test_the_note_is_frozen_so_the_prompt_cache_holds() -> None:
    mw = _mw()
    first = mw._apply(_request([HumanMessage("hi")])).system_message.content_blocks[-1]["text"]
    history = [
        AIMessage("", tool_calls=[{"id": "1", "name": "playwright_browser_click", "args": {}}])
    ]
    later = mw._apply(_request(history)).system_message.content_blocks[-1]["text"]
    assert first == later


# ── the canonical names and the full-roster deferral ───────────────────────


def test_the_search_tools_are_named_search_tools_and_skills_search() -> None:
    """The two progressive-disclosure entry points are always bound."""
    assert {"search_tools", "skills_search"} <= CORE_TOOLS
    assert mw_tool_name() == "search_tools"


def mw_tool_name() -> str:
    return _mw().tools[0].name


def test_a_result_from_the_old_name_still_loads() -> None:
    """Checkpoints written before the rename keep working."""
    mw = _mw()
    mw._apply(_request([HumanMessage("hi")]))
    result = mw.tools[0].invoke({"query": "playwright_browser_click"})
    history = [
        AIMessage("", tool_calls=[{"id": "1", "name": "tool_search", "args": {}}]),
        ToolMessage(result, tool_call_id="1", name="tool_search"),
    ]
    assert "playwright_browser_click" in _names(mw._apply(_request(history)))


def test_the_whole_subagent_roster_is_deferred() -> None:
    """core_agent must hide every subagent except LISTED_SUBAGENTS.

    Read as text: importing core_agent pulls the full agent stack.
    """
    from pathlib import Path

    import novacode_cli
    from novacode_cli.agents.default_subagents.subagents import LISTED_SUBAGENTS

    assert "general-purpose" in LISTED_SUBAGENTS
    source = (
        Path(novacode_cli.__file__).parent / "agents" / "core_agent.py"
    ).read_text(encoding="utf-8")
    assert "s[\"name\"] not in LISTED_SUBAGENTS" in source, (
        "the full subagent roster is still billed in the task description"
    )
