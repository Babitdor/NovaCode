"""Usage attribution tree — per-turn LLM usage attribution.

Covers the tree structure (record/node_for/totals/serialization), the scope
stack (``usage_scope`` / ``scoped_stream``), the model callback handler, the
``build_chat_model`` attach path, cost estimation, durable persistence, and the
end-to-end wiring (``iterate_agent_events`` enters the ``"main"`` scope and the
callback records into the active tree).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

import novacode_cli.ui_events as ev
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.tracking import usage_tree as ut


class FakeStore:
    """Minimal async store stand-in (same shape as other tracking tests)."""

    def __init__(self) -> None:
        self.data: dict[tuple, dict] = {}

    async def aget(self, namespace: tuple, key: str) -> SimpleNamespace | None:
        ns = self.data.get(tuple(namespace), {})
        return SimpleNamespace(value=dict(ns[key])) if key in ns else None

    async def aput(self, namespace: tuple, key: str, value: dict) -> None:
        self.data.setdefault(tuple(namespace), {})[key] = dict(value)

    async def asearch(self, namespace_prefix: tuple) -> list[SimpleNamespace]:
        prefix = tuple(namespace_prefix)
        out = []
        for ns, items in self.data.items():
            if ns[: len(prefix)] == prefix:
                out.extend(SimpleNamespace(key=k, value=dict(v)) for k, v in items.items())
        return out

    async def adelete(self, namespace: tuple, key: str) -> None:
        self.data.get(tuple(namespace), {}).pop(key, None)


def _fake_response(usage: dict | None = None) -> LLMResult:
    msg = AIMessage(
        content="ok",
        usage_metadata=usage
        or {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "total_tokens": 165,
        },
    )
    return LLMResult(generations=[[ChatGeneration(message=msg, text="ok")]], llm_output={})


# ── Tree structure ──────────────────────────────────────────────────────────


class TestUsageTree:
    def test_record_accumulates_and_links(self) -> None:
        tree = ut.UsageTree("t1")
        tree.record(("main",), "gpt-5", {"input_tokens": 10, "output_tokens": 5})
        tree.record(("main",), "gpt-5", {"input_tokens": 20, "output_tokens": 7})
        tree.record(("verification", "main"), "gpt-5", {"input_tokens": 3, "output_tokens": 1})

        main = tree.node_for(("main",))
        assert main.input_tokens == 30
        assert main.output_tokens == 12
        assert main.calls == 2
        assert main.parent is tree.root

        verif = tree.node_for(("verification",))
        assert verif.children[0].scope == "main"
        assert verif.children[0].parent is verif

    def test_totals_roll_up(self) -> None:
        tree = ut.UsageTree("t1")
        tree.record(("main",), "m", {"input_tokens": 10, "output_tokens": 5})
        tree.record(("verification",), "m", {"input_tokens": 2, "output_tokens": 1})
        tree.record(("verification", "main"), "m", {"input_tokens": 3, "output_tokens": 1})
        total = tree.root.totals()
        assert total["input_tokens"] == 15
        assert total["output_tokens"] == 7
        assert total["calls"] == 3

    def test_round_trip(self) -> None:
        tree = ut.UsageTree("t1", started_at=123.0)
        tree.record(("main",), "gpt-5", {"input_tokens": 10, "output_tokens": 5})
        tree.record(("verification", "main"), "gpt-5", {"input_tokens": 3, "output_tokens": 1})
        restored = ut.UsageTree.from_dict(tree.to_dict())
        assert restored.thread_id == "t1"
        assert restored.started_at == 123.0
        assert restored.node_for(("main",)).input_tokens == 10
        assert restored.node_for(("verification", "main")).output_tokens == 1
        assert restored.node_for(("verification", "main")).parent.scope == "verification"


# ── Scope stack ────────────────────────────────────────────────────────────


class TestUsageScope:
    def test_pushes_and_restores(self) -> None:
        tree = ut.UsageTree("t1")
        ut.set_current_tree(tree)
        try:
            with ut.usage_scope("main"):
                assert ut._scope_stack.get() == ("main",)
                with ut.usage_scope("verification"):
                    assert ut._scope_stack.get() == ("main", "verification")
            assert ut._scope_stack.get() == ()
        finally:
            ut.set_current_tree(None)

    def test_noop_without_tree(self) -> None:
        ut.set_current_tree(None)
        with ut.usage_scope("main"):
            assert ut._scope_stack.get() == ()

    async def test_scoped_stream_records_path(self) -> None:
        tree = ut.UsageTree("t1")
        ut.set_current_tree(tree)
        try:

            async def _gen() -> AsyncIterator[int]:
                yield 1
                yield 2

            out = [x async for x in ut.scoped_stream(_gen(), "main")]
            assert out == [1, 2]
            assert tree.node_for(("main",)).scope == "main"
        finally:
            ut.set_current_tree(None)


# ── Callback ───────────────────────────────────────────────────────────────


class TestUsageCallback:
    def test_records_into_current_scope(self) -> None:
        tree = ut.UsageTree("t1")
        ut.set_current_tree(tree)
        try:
            handler = ut.UsageCallbackHandler()
            with ut.usage_scope("main"):
                handler.on_llm_start(
                    {"name": "ChatOpenAI", "kwargs": {"model": "gpt-5"}}, [], run_id=1
                )
                handler.on_llm_end(_fake_response(), run_id=1)
            node = tree.node_for(("main",))
            assert node.model == "gpt-5"
            assert node.input_tokens == 100
            assert node.output_tokens == 50
            assert node.cache_read_tokens == 10
            assert node.cache_creation_tokens == 5
            assert node.calls == 1
        finally:
            ut.set_current_tree(None)

    def test_noop_without_tree(self) -> None:
        ut.set_current_tree(None)
        handler = ut.UsageCallbackHandler()
        handler.on_llm_start({"name": "ChatOpenAI", "kwargs": {"model": "gpt-5"}}, [], run_id=1)
        handler.on_llm_end(_fake_response(), run_id=1)  # must not raise

    def test_llm_output_fallback(self) -> None:
        tree = ut.UsageTree("t1")
        ut.set_current_tree(tree)
        try:
            handler = ut.UsageCallbackHandler()
            resp = LLMResult(
                generations=[[ChatGeneration(message=AIMessage(content="ok"))]],
                llm_output={
                    "model_name": "claude-sonnet-4-5",
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 1,
                    },
                },
            )
            with ut.usage_scope("hermes"):
                handler.on_llm_start({}, [], run_id=2)
                handler.on_llm_end(resp, run_id=2)
            node = tree.node_for(("hermes",))
            assert node.input_tokens == 10  # Raw Anthropic input excludes the cache subsets.
            assert node.output_tokens == 3
            assert node.cache_read_tokens == 2
        finally:
            ut.set_current_tree(None)

    def test_attach_appends_handler(self) -> None:
        class FakeModel:
            def __init__(self) -> None:
                self.callbacks: list = []

        model = FakeModel()
        ut.attach_usage_callback(model)  # type: ignore[arg-type]
        assert ut._HANDLER in model.callbacks
        ut.attach_usage_callback(model)  # type: ignore[arg-type]
        assert model.callbacks.count(ut._HANDLER) == 1  # no duplicates


# ── Cost ────────────────────────────────────────────────────────────────────


class TestEstimateCost:
    def test_known_model(self) -> None:
        # gpt-5: $1.25/M in, $10/M out → 1M in + 1M out = $11.25
        assert ut.estimate_cost(1_000_000, 1_000_000, "gpt-5") == pytest.approx(11.25)

    def test_most_specific_wins(self) -> None:
        # "gpt-5-mini" must match the mini price, not the gpt-5 price.
        assert ut.estimate_cost(1_000_000, 0, "gpt-5-mini") == pytest.approx(0.25)

    def test_unknown_or_local_price_is_unavailable(self) -> None:
        assert ut.estimate_cost(1_000_000, 1_000_000, "qwen3-coder:480b-cloud") is None
        assert ut.estimate_cost(1_000_000, 1_000_000, "") is None


# ── Persistence + report ──────────────────────────────────────────────────


class TestPersistence:
    async def test_round_trip(self) -> None:
        store = FakeStore()
        tree = ut.UsageTree("t1", started_at=100.0)
        tree.record(("main",), "gpt-5", {"input_tokens": 10, "output_tokens": 5})
        await ut.persist_usage_tree(tree, store=store)

        trees = await ut.load_usage_trees("t1", store=store)
        assert len(trees) == 1
        assert trees[0]["root"]["children"][0]["scope"] == "main"

    async def test_filters_by_thread_and_sorts_newest_first(self) -> None:
        store = FakeStore()
        for ts in (200.0, 100.0):
            await ut.persist_usage_tree(ut.UsageTree("t1", started_at=ts), store=store)
        await ut.persist_usage_tree(ut.UsageTree("t2", started_at=300.0), store=store)

        trees = await ut.load_usage_trees("t1", store=store)
        assert [t["started_at"] for t in trees] == [200.0, 100.0]

    def test_format_report(self) -> None:
        tree = ut.UsageTree("t1", started_at=0.0)
        tree.record(("main",), "gpt-5", {"input_tokens": 100, "output_tokens": 50})
        report = ut.format_usage_report([tree.to_dict()])
        assert "main" in report
        assert "total" in report
        assert ut.format_usage_report([]) == "No usage data recorded yet for this session."


# ── End-to-end wiring: iterate_agent_events enters the "main" scope ────────


class _Chunk:
    """Fake AIMessageChunk (name must NOT be 'AIMessage')."""

    def __init__(self, mid: str, blocks: list) -> None:
        self.id = mid
        self._blocks = blocks
        self.usage_metadata = {"input_tokens": 12, "output_tokens": 4}

    @property
    def content_blocks(self) -> list:
        return self._blocks


class _State:
    def __init__(self, msgs: list) -> None:
        self.values = {"messages": msgs}


class _SessionState:
    thread_id = "t1"


class TestWiring:
    async def test_iterate_agent_events_records_main_scope(self) -> None:
        class Agent:
            async def aget_state(self, _config: dict) -> _State:
                return _State([])

            async def astream(self, _inp: object, **_kw: object) -> AsyncIterator[tuple]:
                # Simulate the model callback firing during the stream.
                ut._HANDLER.on_llm_start(
                    {"name": "ChatOpenAI", "kwargs": {"model": "gpt-5"}}, [], run_id=99
                )
                yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "hi"}]), {}))
                ut._HANDLER.on_llm_end(_fake_response(), run_id=99)

        tree = ut.UsageTree("t1")
        ut.set_current_tree(tree)
        try:
            events = [
                e async for e in iterate_agent_events("hi", Agent(), "nova-agent", _SessionState())
            ]
            assert any(isinstance(e, ev.Done) for e in events)
            main = tree.node_for(("main",))
            assert main.scope == "main"
            assert main.input_tokens == 100  # recorded via the callback
            assert main.model == "gpt-5"
        finally:
            ut.set_current_tree(None)
