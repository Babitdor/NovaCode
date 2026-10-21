"""Independent search semantics, time bounds and progressive schema loading."""

from types import SimpleNamespace
from typing import Annotated

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import Field, ValidationError

from novacode_cli.agents.tool_search import ToolSearchMiddleware, loaded_names
from novacode_cli.agents.tool_search_variants import BM25_TOOL, REGEX_TOOL, regex_matches


@tool
def database_records(
    collection_id: Annotated[str, Field(description="Solstice collection identifier")],
) -> str:
    """Query records from a database collection."""
    return collection_id


@tool
def browser_click(element: str) -> str:
    """Click a visible browser element."""
    return element


def prepare():
    middleware = ToolSearchMiddleware(
        deferred_subagents={"sql-reviewer": "Reviews database queries"}
    )
    middleware._frequent = set()
    request = SimpleNamespace(
        tools=[database_records, browser_click, *middleware.tools],
        messages=[HumanMessage("hi")],
        state={},
        system_message=SystemMessage(""),
    )
    request.override = lambda **changes: SimpleNamespace(**{**vars(request), **changes})
    middleware._apply(request)
    return middleware, request


@pytest.mark.parametrize("name", [REGEX_TOOL, BM25_TOOL])
def test_results_load_real_schemas_and_parameter_descriptions(name):
    middleware, request = prepare()
    search = next(t for t in middleware.tools if t.name == name)
    result = search.invoke({"query": "Solstice"})
    assert "database_records" in result
    history = [ToolMessage(result, tool_call_id="search", name=name)]
    assert "database_records" in loaded_names(history)
    bound = middleware._apply(request.override(messages=history)).tools
    assert database_records in bound


def test_regex_is_case_insensitive_and_searches_independent_fields():
    middleware, _ = prepare()
    result = middleware._regex_search(r"^BROWSER_.*click$")
    assert "Loaded tools: browser_click" in result
    assert "database_records" not in result
    result = middleware._regex_search("sql-reviewer")
    assert "subagent `sql-reviewer`" in result


@pytest.mark.parametrize("pattern", ["(", "a" * 201])
def test_invalid_patterns_never_load_tools(pattern):
    middleware, _ = prepare()
    result = middleware._regex_search(pattern)
    assert result.startswith("Tool search failed:")
    assert not loaded_names([ToolMessage(result, tool_call_id="s", name=REGEX_TOOL)])


def test_pathological_regex_has_a_time_limit():
    with pytest.raises(TimeoutError):
        regex_matches([{"name": "test", "description": "a" * 100000 + "!"}], r"(a|aa)+$")


def test_bm25_ranks_keyword_matches_without_embeddings(monkeypatch):
    import novacode_cli.skills.retrieval as retrieval

    monkeypatch.setattr(
        retrieval, "_model", lambda: pytest.fail("BM25 attempted embedding discovery")
    )
    middleware, _ = prepare()
    result = middleware._bm25_search("query database collection records")
    assert result.splitlines()[1].startswith("- `database_records`")
    assert "No registered tools match" in middleware._bm25_search("zzzzunfindablezzzz")
    assert "No registered tools match" in middleware._bm25_search("the and or")
    assert "Tool search failed" in middleware._bm25_search("x" * 501)


@pytest.mark.parametrize("name", [REGEX_TOOL, BM25_TOOL])
@pytest.mark.parametrize(
    "arguments", [{"query": 2}, {"query": "tool", "limit": "2"}, {"query": "tool", "offset": True}]
)
def test_search_arguments_never_coerce_strings_or_booleans(name, arguments):
    middleware, _ = prepare()
    search = next(t for t in middleware.tools if t.name == name)
    with pytest.raises(ValidationError):
        search.invoke(arguments)


def test_bm25_pagination_covers_all_matches_and_rebuilds_for_inventory_change():
    middleware, _ = prepare()
    middleware._catalog = [
        {"name": f"service_{n}", "description": "service actions", "kind": "tool"} for n in range(9)
    ]
    seen = []
    for offset in (0, 3, 6):
        result = middleware._bm25_search("service", limit=3, offset=offset)
        seen.extend(loaded_names([ToolMessage(result, name=BM25_TOOL, tool_call_id="s")]))
    assert len(set(seen)) == 9
    middleware._catalog = [{"name": "replacement", "description": "service action", "kind": "tool"}]
    assert "Loaded tools: replacement" in middleware._bm25_search("service")
