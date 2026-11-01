"""Normal frontend turns own usage persistence; nested team turns reuse it."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.core import agent_loop
from novacode_cli.tracking import usage_tree as ut

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


@pytest.mark.parametrize("close_early", [False, True])
async def test_normal_turn_persists_and_restores_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    close_early: bool,
) -> None:
    persist = AsyncMock()
    monkeypatch.setattr(ut, "persist_usage_tree", persist)
    ut.set_current_tree(None)

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        tree = ut.get_current_tree()
        assert tree.thread_id == "session"
        tree.record(("main",), "test", {"input_tokens": 1})
        yield "first"
        yield "last"

    monkeypatch.setattr(agent_loop, "_iterate_agent_events", events)
    stream = agent_loop.iterate_agent_events(
        "hello", None, None, SimpleNamespace(thread_id="session")
    )
    if close_early:
        assert await anext(stream) == "first"
        await stream.aclose()
    else:
        assert [event async for event in stream] == ["first", "last"]
    assert ut.get_current_tree() is None
    persist.assert_awaited_once()
    assert persist.call_args.args[0].root.totals()["calls"] == 1


async def test_team_owned_tree_is_neither_replaced_nor_persisted_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist = AsyncMock()
    monkeypatch.setattr(ut, "persist_usage_tree", persist)
    tree = ut.UsageTree("team")

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        assert ut.get_current_tree() is tree
        yield "done"

    monkeypatch.setattr(agent_loop, "_iterate_agent_events", events)
    ut.set_current_tree(tree)
    try:
        assert [
            event
            async for event in agent_loop.iterate_agent_events(
                "hello", None, None, SimpleNamespace(thread_id="team")
            )
        ] == ["done"]
        assert ut.get_current_tree() is tree
        persist.assert_not_awaited()
    finally:
        ut.set_current_tree(None)


async def test_back_to_back_turns_have_distinct_durable_keys():
    store = SimpleNamespace(aput=AsyncMock())
    trees = [ut.UsageTree("thread", started_at=10.0) for _ in range(2)]
    for tree in trees:
        await ut.persist_usage_tree(tree, store)
    keys = [call.args[1] for call in store.aput.call_args_list]
    assert len(set(keys)) == 2


async def test_cross_task_close_restores_both_contexts_and_persists_each_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist = AsyncMock()
    monkeypatch.setattr(ut, "persist_usage_tree", persist)
    ut.set_current_tree(None)
    seen = []

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        tree = ut.get_current_tree()
        tree.record(("main",), "test", {"input_tokens": 1})
        seen.append(tree)
        try:
            yield "first"
        finally:
            assert ut.get_current_tree() is tree

    monkeypatch.setattr(agent_loop, "_iterate_agent_events", events)

    async def close_from_another_context(stream: AsyncGenerator[str, None]) -> None:
        closer_tree = ut.UsageTree("closer")
        ut.set_current_tree(closer_tree)
        await stream.aclose()
        assert ut.get_current_tree() is closer_tree

    for _ in range(2):
        stream = agent_loop.iterate_agent_events(
            "hello", None, None, SimpleNamespace(thread_id="session")
        )
        assert await anext(stream) == "first"
        assert ut.get_current_tree() is None
        await asyncio.create_task(close_from_another_context(stream))
        assert ut.get_current_tree() is None
    assert persist.await_count == 2
    assert seen[0] is not seen[1]


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_failed_advance_restores_context_and_persists_once(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    persist = AsyncMock()
    monkeypatch.setattr(ut, "persist_usage_tree", persist)
    ut.set_current_tree(None)

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        tree = ut.get_current_tree()
        tree.record(("main",), "test", {"input_tokens": 1})
        raise error
        yield "unreachable"

    monkeypatch.setattr(agent_loop, "_iterate_agent_events", events)
    stream = agent_loop.iterate_agent_events(
        "hello", None, None, SimpleNamespace(thread_id="session")
    )
    with pytest.raises(type(error)):
        await anext(stream)
    assert ut.get_current_tree() is None
    persist.assert_awaited_once()


@pytest.mark.parametrize("cross_task_close", [False, True])
async def test_scoped_model_stream_restores_scopes_and_records_final_callback(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cross_task_close: bool,
) -> None:
    persist = AsyncMock()
    monkeypatch.setattr(ut, "persist_usage_tree", persist)
    ut.set_current_tree(None)
    handler = ut.UsageCallbackHandler()
    scope_token = ut._scope_stack.set(("caller",))

    async def model_events() -> AsyncIterator[str]:
        assert ut._scope_stack.get() == ("caller", "main")
        handler.on_llm_start({}, [], run_id="model")
        try:
            yield "first"
            assert ut._scope_stack.get() == ("caller", "main")
            yield "last"
        finally:
            assert ut.get_current_tree() is not None
            assert ut._scope_stack.get() == ("caller", "main")
            handler.on_llm_end(
                SimpleNamespace(llm_output={"usage": {"input_tokens": 10, "output_tokens": 2}}),
                run_id="model",
            )

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        async with aclosing(ut.scoped_stream(model_events(), "main")) as source:
            async for event in source:
                yield event

    monkeypatch.setattr(agent_loop, "_iterate_agent_events", events)

    async def close_from_other_scope(stream: AsyncGenerator[str, None]) -> None:
        closing_tree = ut.UsageTree("closing-task")
        ut.set_current_tree(closing_tree)
        ut._scope_stack.set(("closing-task",))
        await stream.aclose()
        assert ut.get_current_tree() is closing_tree
        assert ut._scope_stack.get() == ("closing-task",)

    try:
        stream = agent_loop.iterate_agent_events(
            "hello", None, None, SimpleNamespace(thread_id="session")
        )
        assert await anext(stream) == "first"
        assert ut.get_current_tree() is None
        assert ut._scope_stack.get() == ("caller",)
        if cross_task_close:
            await asyncio.create_task(close_from_other_scope(stream))
        else:
            assert [event async for event in stream] == ["last"]
        assert ut.get_current_tree() is None
        assert ut._scope_stack.get() == ("caller",)
        persist.assert_awaited_once()
        tree = persist.call_args.args[0]
        assert tree.node_for(("caller", "main")).input_tokens == 10
        assert tree.requests[0]["scope"] == ["caller", "main"]
        assert not handler._runs
    finally:
        ut._scope_stack.reset(scope_token)
