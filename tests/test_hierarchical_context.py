"""Reversible model-authored compaction, including real graph checkpoint/resume."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict
from langgraph.checkpoint.memory import InMemorySaver

from novacode_cli.agents.context_compaction import ContextCompactionMiddleware
from novacode_cli.context.hierarchical import (
    acp_status,
    compress_range,
    decompress,
    project,
    ref,
    restore,
    search_context,
)
from tests.test_context_compaction import Worker, _well_formed

if TYPE_CHECKING:
    from pathlib import Path


SUMMARY = (
    "Completed investigation. Keep the user constraints. "
    "Verified token ALPHA-987 in the tool result; tests remain pending."
)


def history():
    return [
        HumanMessage(content="Never modify production. Keep exact constraints.", id="user"),
        AIMessage(
            content="",
            id="call",
            tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "c"}],
        ),
        ToolMessage(
            content="ALPHA-987\n" + "source code and findings\n" * 900,
            tool_call_id="c",
            id="result",
        ),
        AIMessage(content="investigated", id="done"),
        HumanMessage(content="Continue with the fix", id="new-user"),
        AIMessage(content="working", id="working"),
        HumanMessage(content="Use tests", id="tail"),
    ]


def folded():
    messages = history()
    update = compress_range(
        {"messages": messages}, ref(messages[0]), ref(messages[3]), SUMMARY, "compress-1"
    )
    return {"messages": messages, **update}


def test_fold_preserves_constraints_pairs_originals_and_stable_prefix():
    state = folded()
    view = project(state["messages"], state)
    _well_formed(view)
    assert view[1] == state["messages"][0]
    assert "source code" not in str(view)
    assert len(state["messages"]) == 7
    block = next(iter(state["_context_blocks"]))
    raw = restore(state, block, limit=20000)
    chunks = raw["content"]
    while raw["next_offset"] is not None:
        raw = restore(state, block, raw["next_offset"], 20000)
        chunks += raw["content"]
    import json

    assert messages_from_dict(json.loads(chunks)) == state["messages"][:4]
    extended = project([*state["messages"], AIMessage(content="later", id="later")], state)
    assert extended[:-1] == view


@pytest.mark.parametrize(
    ("start", "end", "error"), [(1, 1, "exchanges"), (2, 3, "exchanges"), (0, 6, "protected")]
)
def test_invalid_ranges_do_not_mutate(start: int, end: int, error: str):
    messages = history()
    state = {"messages": messages}
    with pytest.raises(ValueError, match=error):
        compress_range(state, ref(messages[start]), ref(messages[end]), SUMMARY, "bad")
    assert set(state) == {"messages"}


def test_parallel_compression_is_rejected_without_losing_an_update():
    messages = history()
    messages.append(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "compress", "args": {}, "id": "one"},
                {"name": "compress", "args": {}, "id": "two"},
            ],
        )
    )
    with pytest.raises(ValueError, match="parallel"):
        compress_range({"messages": messages}, ref(messages[0]), ref(messages[3]), SUMMARY, "one")


def test_nested_blocks_remain_recoverable_after_message_replacement():
    state = folded()
    messages = state["messages"]
    # Add completed raw material beyond the first block and keep a recent tail.
    messages[4:4] = [AIMessage(content="new evidence\n" * 800, id="additional")]
    block = next(iter(state["_context_blocks"]))
    update = compress_range(state, block, ref(messages[4]), SUMMARY, "compress-2")
    state.update(update)
    parent = next(reversed(state["_context_blocks"]))
    assert state["_context_blocks"][parent]["tier"] == 2
    assert not state["_context_blocks"][block]["active"]
    assert len(project(messages, state)) < len(messages)
    state["messages"] = [HumanMessage(content="manual compact summary", id="new")]
    assert "ALPHA-987" in restore(state, parent)["content"]
    assert "ALPHA-987" in restore(state, block)["content"]


def test_tools_are_scoped_paginated_and_schema_hides_runtime():
    state = folded()
    runtime = SimpleNamespace(state=state)
    found = search_context.func("ALPHA-987", runtime)
    assert found["matches"]
    assert any(m["blocks"] for m in found["matches"])
    assert acp_status.func(runtime, limit=2)["next_offset"] == 2
    assert decompress.func("unknown", runtime)["error"]
    assert not search_context.func("ALPHA-987", SimpleNamespace(state={"messages": []}))["matches"]
    assert "runtime" not in acp_status.tool_call_schema.model_json_schema()["properties"]


class Compressor(Worker):
    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):  # noqa: ANN202, ANN401, ARG002 - scripted model protocol
        from langchain_core.outputs import ChatGeneration, ChatResult

        self.seen.append(list(messages))
        if not any(
            isinstance(m, ToolMessage) and "Compressed as" in str(m.content) for m in messages
        ):
            originals = history()
            result = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "compress",
                        "args": {
                            "start_ref": ref(originals[0]),
                            "end_ref": ref(originals[3]),
                            "summary": SUMMARY,
                        },
                        "id": "real-compress",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            result = AIMessage(content="continued with the compressed context")
        return ChatResult(generations=[ChatGeneration(message=result)])


def test_graph_persists_blocks_and_archive_across_resume_and_isolates_threads():
    model = Compressor()
    saver = InMemorySaver()
    graph = create_agent(
        model, middleware=[ContextCompactionMiddleware(context_window=100000)], checkpointer=saver
    )
    config = {"configurable": {"thread_id": "compressed"}}
    graph.invoke({"messages": history()}, config)
    state = graph.get_state(config).values
    assert state["_context_blocks"]
    assert "source code" not in str(model.seen[-1])
    _well_formed(model.seen[-1])
    resumed = create_agent(
        model, middleware=[ContextCompactionMiddleware(context_window=100000)], checkpointer=saver
    )
    resumed.invoke({"messages": [HumanMessage(content="Resume", id="resume")]}, config)
    assert "source code" not in str(model.seen[-1])
    block = next(iter(state["_context_blocks"]))
    assert "ALPHA-987" in restore(resumed.get_state(config).values, block)["content"]
    assert not resumed.get_state({"configurable": {"thread_id": "other"}}).values


@pytest.mark.asyncio
async def test_async_graph_executes_compression_on_its_owning_state():
    model = Compressor()
    graph = create_agent(
        model,
        middleware=[ContextCompactionMiddleware(context_window=100000)],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "async"}}
    await graph.ainvoke({"messages": history()}, config)
    assert (await graph.aget_state(config)).values["_context_blocks"]
    assert "source code" not in str(model.seen[-1])


def test_sqlite_reopen_retains_exact_archive(tmp_path: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    config = {"configurable": {"thread_id": "disk"}}
    path = str(tmp_path / "checkpoint.sqlite")
    with SqliteSaver.from_conn_string(path) as saver:
        graph = create_agent(
            Compressor(),
            middleware=[ContextCompactionMiddleware(context_window=100000)],
            checkpointer=saver,
        )
        graph.invoke({"messages": history()}, config)
    with SqliteSaver.from_conn_string(path) as saver:
        graph = create_agent(
            Compressor(),
            middleware=[ContextCompactionMiddleware(context_window=100000)],
            checkpointer=saver,
        )
        state = graph.get_state(config).values
        block = next(iter(state["_context_blocks"]))
        assert "ALPHA-987" in restore(state, block)["content"]
        assert "source code" not in str(project(state["messages"], state))


@pytest.mark.asyncio
async def test_automatic_compaction_after_fold_uses_original_cutoff(
    monkeypatch: pytest.MonkeyPatch,
):
    from langchain.agents.middleware.types import ModelRequest

    from novacode_cli.agents.context_compaction import effective_view

    state = folded()
    state["messages"][4:4] = [AIMessage(content="additional completed work\n" * 900, id="large")]
    model = Worker()
    middleware = ContextCompactionMiddleware(model, context_window=4000)
    request = ModelRequest(
        model=model, messages=state["messages"], state=state, system_message=None, tools=[]
    )
    plan = middleware._plan(request, force=True)
    assert plan.cutoff

    async def summarize(*_args: object) -> str:
        return SUMMARY

    async def archive(*_args: object) -> str:
        return "test-archive"

    monkeypatch.setattr(middleware, "_summarize", summarize)
    monkeypatch.setattr(middleware, "_archive", archive)
    sent, update = await middleware._compact(request, plan, overflow=True)
    reconstructed, _ = effective_view(state["messages"], update["_summarization_event"])
    assert [m.id for m in reconstructed] == [m.id for m in sent]
    assert "ALPHA-987" in restore(state, next(iter(state["_context_blocks"])))["content"]


def test_context_tools_stay_direct_and_available_in_code_mode():
    from novacode_cli.agents.code_mode import DIRECT_ONLY_TOOLS
    from novacode_cli.agents.tool_search import CORE_TOOLS
    from novacode_cli.context.hierarchical import TOOLS

    assert TOOLS <= DIRECT_ONLY_TOOLS
    assert TOOLS <= CORE_TOOLS


def test_deepagent_replaces_library_summarizer_and_exposes_context_tools():
    from deepagents import create_deep_agent

    model = Compressor()
    graph = create_deep_agent(
        model=model,
        middleware=[ContextCompactionMiddleware(context_window=100000)],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "deep"}}
    graph.invoke({"messages": history()}, config)
    assert graph.get_state(config).values["_context_blocks"]
    assert "source code" not in str(model.seen[-1])


def test_fallback_cannot_split_a_fold_from_its_preserved_user_message():
    from novacode_cli.context.hierarchical import safe_cutoff

    state = folded()
    view = project(state["messages"], state)
    assert safe_cutoff(view, 1, state) == 0
    assert safe_cutoff(view, 2, state) == 2


def test_partial_overlap_and_no_savings_leave_archive_unchanged():
    state = folded()
    view = project(state["messages"], state)
    with pytest.raises(ValueError, match="partially overlaps"):
        compress_range(state, ref(view[1]), ref(view[1]), SUMMARY, "overlap")
    messages = history()
    messages[2].content = "short result"
    with pytest.raises(ValueError, match="save enough"):
        compress_range(
            {"messages": messages}, ref(messages[0]), ref(messages[3]), SUMMARY, "no-saving"
        )
    assert len(state["_context_blocks"]) == 1


def test_reused_provider_call_id_does_not_overwrite_an_older_block():
    state = folded()
    first = next(iter(state["_context_blocks"]))
    messages = state["messages"]
    messages[4:4] = [AIMessage(content="more completed evidence\n" * 800, id="more")]
    state.update(compress_range(state, first, ref(messages[4]), SUMMARY, "compress-1"))
    assert len(state["_context_blocks"]) == 2
    assert "ALPHA-987" in restore(state, first)["content"]
