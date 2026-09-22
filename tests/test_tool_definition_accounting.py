"""Tool-schema cost must actually reach the context breakdown.

Tool definitions ship on every request but live on the bound model, not in the
message list, so ``build_context_breakdown`` counted them as zero — hiding a
large, permanent slice of the baseline. These prove the count is real, using
genuine ``BaseTool`` objects rather than stand-ins, and that the two accessors
used by the REPL can find the tool list.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

from novacode_cli.context._analysis import _tool_schema_text, build_context_breakdown
from novacode_cli.ui.execution import _bound_tools

MODEL = "claude-opus-4-8"


@tool
def search_files(pattern: str, max_results: int = 10) -> str:
    """Search the workspace for files matching a glob pattern."""
    return ""


@tool
def read_config(path: str) -> str:
    """Read and return the contents of a configuration file at the given path."""
    return ""


# -- the counting function --------------------------------------------------


def test_real_tools_produce_a_non_empty_schema_text() -> None:
    text = _tool_schema_text([search_files, read_config])
    assert text
    # Names, descriptions and argument names all belong in the cost.
    assert "search_files" in text
    assert "Search the workspace" in text
    assert "pattern" in text
    assert "max_results" in text


def test_schema_text_grows_with_each_tool() -> None:
    one = len(_tool_schema_text([search_files]))
    two = len(_tool_schema_text([search_files, read_config]))
    assert two > one


def test_dict_shaped_tools_are_supported() -> None:
    """Some call sites pass plain dicts (OpenAI-style schemas)."""
    text = _tool_schema_text(
        [{"name": "fetch", "description": "Fetch a URL", "parameters": {"type": "object"}}]
    )
    assert "fetch" in text
    assert "Fetch a URL" in text
    assert "object" in text


def test_no_tools_yields_empty_text() -> None:
    assert _tool_schema_text([]) == ""


# -- it reaches the breakdown ----------------------------------------------


def test_tool_definitions_tokens_is_populated() -> None:
    bd = build_context_breakdown([], MODEL, tools=[search_files, read_config])
    assert bd.tool_definitions_tokens > 0, "tool schemas were counted as zero"


def test_tool_definitions_tokens_is_included_in_the_total() -> None:
    """A permanent baseline cost must not be excluded from the total."""
    bd = build_context_breakdown([], MODEL, tools=[search_files])
    assert bd.total_tokens == bd.tool_definitions_tokens
    assert bd.baseline_tokens >= bd.tool_definitions_tokens


def test_omitting_tools_keeps_the_old_zero_behaviour() -> None:
    """Backward compatible: no tools supplied -> 0, not a crash."""
    bd = build_context_breakdown([], MODEL)
    assert bd.tool_definitions_tokens == 0


def test_more_tools_means_a_larger_total() -> None:
    few = build_context_breakdown([], MODEL, tools=[search_files]).total_tokens
    many = build_context_breakdown([], MODEL, tools=[search_files, read_config]).total_tokens
    assert many > few


# -- the accessors the REPL uses -------------------------------------------


def test_bound_tools_finds_a_real_tool_node() -> None:
    """``_bound_tools`` walks graph nodes to a genuine ``ToolNode``."""
    node = ToolNode([search_files, read_config])
    agent = SimpleNamespace(nodes={"tools": SimpleNamespace(bound=node)})

    found = _bound_tools(agent)

    assert found is not None
    assert {t.name for t in found} == {"search_files", "read_config"}


def test_bound_tools_returns_none_when_absent() -> None:
    """Accounting only — a miss degrades to 0, never raises."""
    assert _bound_tools(SimpleNamespace(nodes={})) is None
    assert _bound_tools(object()) is None


def test_bound_tools_survives_a_hostile_graph() -> None:
    class _Exploding:
        @property
        def nodes(self):
            raise RuntimeError("boom")

    assert _bound_tools(_Exploding()) is None


def test_breakdown_accepts_whatever_either_accessor_returns() -> None:
    """The call site feeds either source into the same parameter.

    ``session_state._tools`` and ``_bound_tools`` both yield a list of tool
    objects (or None), and ``build_context_breakdown`` must handle both shapes
    without a caller having to normalise them.
    """
    node = ToolNode([search_files])
    from_graph = _bound_tools(SimpleNamespace(nodes={"tools": SimpleNamespace(bound=node)}))
    assert from_graph is not None

    bd = build_context_breakdown([], MODEL, tools=from_graph)
    assert bd.tool_definitions_tokens > 0

    # None is the documented "unknown" value, not an error.
    assert build_context_breakdown([], MODEL, tools=None).tool_definitions_tokens == 0


def test_session_state_tools_reach_the_breakdown() -> None:
    """The REPL's primary source: ``session_state._tools``, populated at startup.

    ``_tools`` lives on AgentRuntimeState and is delegated through
    SessionState.__getattr__, so this pins the whole chain — the REPL does
    ``getattr(session_state, "_tools", None)``.
    """
    from novacode_cli.states.Session import SessionState

    state = SessionState(auto_approve=True)
    state.set_agent_context(
        agent=None,
        backend=None,
        checkpointer=None,
        store=None,
        tools=[search_files, read_config],
        assistant_id="nova-agent",
        model=None,
    )

    tools = getattr(state, "_tools", None)
    assert tools is not None and len(tools) == 2

    bd = build_context_breakdown([], MODEL, tools=tools)
    assert bd.tool_definitions_tokens > 0, "session_state tools did not reach the count"

