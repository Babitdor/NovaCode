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


def test_available_async_lifecycle_tools_are_bound_without_search() -> None:
    names = {
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
    }
    req = _request([HumanMessage("review this change in the background")])
    req = req.override(tools=[*TOOLS, *[_mcp(name, "Async subagent tool") for name in names]])
    bound = _names(_mw()._apply(req))
    assert names <= bound
    assert "playwright_browser_navigate" not in bound
    # A session without an async server must not acquire nonexistent tools.
    assert not names & _names(_mw()._apply(_request([HumanMessage("hi")])))


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


def test_every_registered_tool_is_searchable_even_when_already_bound() -> None:
    mw = _mw()
    mw._frequent = {"playwright_browser_click"}
    history = [
        AIMessage("", tool_calls=[{"id": "1", "name": "playwright_browser_snapshot", "args": {}}])
    ]
    mw._apply(_request(history))
    for t in TOOLS:
        result = mw.tools[0].invoke({"query": t.name.upper()})
        assert result.splitlines()[0] == f"{LOADED_MARKER} {t.name}"
        assert t.name in loaded_names([ToolMessage(result, name="search_tools", tool_call_id="s")])


def test_search_keeps_loaded_subagents_discoverable() -> None:
    mw = _mw()
    mw._apply(_request([]))
    result = mw._tool_search("rust-reviewer")
    mw._apply(_request([ToolMessage(result, name="search_tools", tool_call_id="s")]))
    assert 'task(subagent_type="rust-reviewer")' in mw._tool_search("rust-reviewer")


@pytest.mark.parametrize("query", ["browser click", "browser_click", "playwright_browser_clik"])
def test_partial_names_and_typos_find_the_tool(query: str) -> None:
    mw = _mw()
    mw._apply(_request([]))
    assert "playwright_browser_click" in mw._tool_search(query).splitlines()[0]


def test_exact_name_does_not_hide_other_requested_capabilities() -> None:
    capture = _mcp("capture_screen", "Capture a screenshot image of the browser.")
    mw = _mw()
    mw._apply(_request([]).override(tools=[*TOOLS, capture]))
    result = mw._tool_search("read_file and capture a screenshot of the browser?")
    assert "read_file" in result.splitlines()[0]
    assert "capture_screen" in result.splitlines()[0]


def test_wildcards_can_page_through_every_tool_and_load_later_results() -> None:
    extras = [_mcp(f"server_action_{i:02d}", "A server action.") for i in range(35)]
    mw = ToolSearchMiddleware()
    mw._apply(_request([]).override(tools=extras))
    found = set()
    history = []
    for offset in range(0, len(extras), 5):
        result = mw.tools[0].invoke({"query": "server_*", "offset": offset})
        message = ToolMessage(result, name="search_tools", tool_call_id=str(offset))
        found.update(loaded_names([message]))
        history.append(message)
    assert found == {t.name for t in extras}
    assert "Showing 1-5 of 35 matches" in mw._tool_search("*", limit=5)
    assert _names(mw._apply(_request(history).override(tools=extras))) == found
    assert "server_action_34" in mw._tool_search("server_action_34")


def test_capability_search_can_retrieve_more_than_twenty_matches() -> None:
    extras = [_mcp(f"integration_{i:02d}", "Encrypt stored backups securely.") for i in range(35)]
    mw = ToolSearchMiddleware()
    mw._apply(_request([]).override(tools=extras))
    result = mw._tool_search("encrypt backups", limit=50)
    assert loaded_names([ToolMessage(result, name="search_tools", tool_call_id="s")]) == {
        t.name for t in extras
    }


def test_catalog_tracks_added_and_removed_tools_even_when_nothing_is_deferred() -> None:
    mw = ToolSearchMiddleware()
    mw._apply(_request([]))
    added = _mcp("new_integration", "Upload a report.")
    updated = mw._apply(_request([]).override(tools=[read_file, added]))
    assert "new_integration" in updated.system_message.content_blocks[-1]["text"]
    assert "new_integration" in mw._tool_search("new_integration")
    mw._apply(_request([]).override(tools=[read_file]))
    assert "playwright_browser_click" not in mw._tool_search("*")
    assert "new_integration" not in mw._tool_search("*")
    assert mw._tool_search("read_file").startswith(LOADED_MARKER)


def test_function_dict_tools_are_discoverable() -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": "checksum_file", "description": "Checksum a file."},
        },
        {"type": "function", "name": "check_signature", "description": "Verify a signature."},
    ]
    mw = ToolSearchMiddleware()
    mw._apply(_request([]).override(tools=tools))
    result = mw._tool_search("`CHECKSUM_FILE`, check_signature")
    assert "checksum_file, check_signature" in result.splitlines()[0]


def test_search_still_works_if_the_retrieval_index_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import novacode_cli.agents.tool_search as mod

    def unavailable(_catalog: list) -> None:
        message = "index unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(mod, "get_index", unavailable)
    mw = _mw()
    mw._apply(_request([]))
    assert "playwright_browser_navigate" in mw._tool_search("navigate to a url")
    assert "playwright_browser_click" in mw._tool_search("playwright_browser_click")
    assert "rust-reviewer" in mw._tool_search("*", limit=50)


def test_search_results_name_the_correct_sync_or_async_dispatch_tool() -> None:
    mw = ToolSearchMiddleware(
        deferred_subagents={
            "sync-review": "Review code now.",
            "background-review": "Review code in the background.",
        },
        async_subagents={"background-review"},
    )
    mw._apply(_request([]))
    assert 'task(subagent_type="sync-review")' in mw._tool_search("sync-review")
    result = mw._tool_search("background-review")
    assert 'start_async_task(subagent_type="background-review")' in result
    assert 'use `task(subagent_type="background-review")' not in result


def test_tool_found_in_structured_message_is_loaded_on_next_call() -> None:
    mw = _mw()
    mw._apply(_request([]))
    result = mw._tool_search("playwright_browser_click")
    message = ToolMessage(
        content=[{"type": "text", "text": result}], name="search_tools", tool_call_id="s"
    )
    assert "playwright_browser_click" in _names(mw._apply(_request([message])))


def test_empty_and_unmatched_queries_offer_catalog_browsing() -> None:
    mw = _mw()
    mw._apply(_request([]))
    for query in ("", " ", "zzzz_unknown_capability", "absent_*"):
        result = mw._tool_search(query)
        assert "browse" in result
        assert not result.startswith(LOADED_MARKER)


def test_page_beyond_last_match_explains_how_to_retry_without_loading() -> None:
    mw = _mw()
    mw._apply(_request([]))
    result = mw._tool_search("playwright_browser_click", offset=1)
    assert "offset smaller than 1" in result
    assert not result.startswith(LOADED_MARKER)


def test_empty_inventory_reports_no_registered_tools() -> None:
    mw = ToolSearchMiddleware()
    mw._apply(_request([]).override(tools=[]))
    assert "No tools are currently registered" in mw._tool_search("*")


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (51, 0), (5, -1)])
def test_invalid_page_parameters_are_rejected(limit: int, offset: int) -> None:
    with pytest.raises(ValueError, match="limit must be"):
        _mw()._tool_search("*", limit=limit, offset=offset)


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
    source = (Path(novacode_cli.__file__).parent / "agents" / "core_agent.py").read_text(
        encoding="utf-8"
    )
    assert 's["name"] not in LISTED_SUBAGENTS' in source, (
        "the full subagent roster is still billed in the task description"
    )
