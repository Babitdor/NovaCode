"""Content-free cache diagnostics across partial usage and concurrent calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from novacode_cli.context.snapshot import ContextSnapshot
from novacode_cli.tracking import usage_tree as ut


def response(normalized: dict | None = None, raw: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        usage_metadata=normalized,
                        response_metadata={"token_usage": raw or {}},
                    )
                )
            ]
        ],
        llm_output={},
    )


def test_partial_normalized_usage_merges_deepseek_raw_cache_fields():
    usage = ut._extract_usage(
        response(
            {"input_tokens": 100, "output_tokens": 5},
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
        )
    )
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_read_tokens": 80,
        "uncached_tokens": 20,
    }


def test_openrouter_write_tokens_are_not_lost():
    usage = ut._extract_usage(response(
        {"input_tokens": 1000, "input_token_details": {"cache_read": 0}},
        {"prompt_tokens": 1000, "prompt_tokens_details": {
            "cached_tokens": 0, "cache_write_tokens": 900,
        }},
    ))
    assert usage["cache_creation_tokens"] == 900
    assert usage["uncached_tokens"] == 100


def test_anthropic_normalized_cache_write_tiers_are_combined():
    usage = ut._extract_usage(response({
        "input_tokens": 1000,
        "input_token_details": {
            "cache_read": 100, "cache_creation": 0,
            "ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 300,
        },
    }))
    assert usage["cache_creation_tokens"] == 700
    assert usage["uncached_tokens"] == 200


def test_missing_cache_writes_and_request_reads_are_not_zero():
    tree = ut.UsageTree("partial")
    tree.record(("main",), "custom", {"input_tokens": 100, "cache_read_tokens": 0})
    tree.requests.append({"provider": "custom", "usage": {"input_tokens": 100}})
    rendered = tree.render()
    assert "writes unavailable" in rendered
    assert "cache read unavailable" in rendered
    assert "0 written" not in rendered
    restored = ut.UsageTree.from_dict(tree.to_dict())
    assert restored.render() == rendered


def test_reported_zero_wins_and_absent_usage_stays_absent():
    assert ut._extract_usage(response()) == {}
    assert ut._extract_usage(response({"input_tokens": 0}, {"prompt_tokens": 20})) == {
        "input_tokens": 0,
    }
    usage = ut._extract_usage(
        response(
            {"input_tokens": 10, "input_token_details": {"cache_read": 0}},
            {"input_tokens": 7, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
        )
    )
    assert usage["input_tokens"] == 10
    assert usage["cache_read_tokens"] == 0
    assert usage["cache_creation_tokens"] == 1


def test_missing_cache_observations_are_unavailable_in_report():
    tree = ut.UsageTree("missing")
    tree.record(("main",), "gpt-5", {"input_tokens": 100, "output_tokens": 1})
    assert "Cache reuse: unavailable" in tree.render()
    tree.record(
        ("main",),
        "gpt-5",
        {
            "input_tokens": 20,
            "output_tokens": 1,
            "cache_read_tokens": 10,
        },
    )
    assert "Cache reuse: 50.0%" in tree.render()
    assert "20/120 observed" in tree.render()
    tree.record(("main",), "gpt-5", {"cache_read_tokens": 1000})
    assert "Cache reuse: 50.0%" in tree.render()


def test_request_bound_preserves_all_aggregated_totals_and_old_records():
    tree = ut.UsageTree("bounded")
    handler = ut.UsageCallbackHandler()
    ut.set_current_tree(tree)
    try:
        for run_id in range(3000):
            handler.on_llm_start({"kwargs": {"model": "gpt-5"}}, [], run_id=run_id)
        for run_id in range(3000):
            handler.on_llm_end(response({"input_tokens": 10, "output_tokens": 1}), run_id=run_id)
    finally:
        ut.set_current_tree(None)
    assert tree.root.totals()["input_tokens"] == 30000
    assert len(tree.requests) == 256
    assert tree.requests_aggregated == 2744
    assert not handler._runs
    restored = ut.UsageTree.from_dict(tree.to_dict())
    assert restored.requests == tree.requests
    assert restored.requests_aggregated == 2744
    assert ut.UsageTree.from_dict({"thread_id": "old", "root": {"scope": "turn"}}).requests == []


async def test_concurrent_calls_keep_starting_scope_and_cleanup_cancelled_runs():
    handler = ut.UsageCallbackHandler()
    trees = [ut.UsageTree("first"), ut.UsageTree("second")]

    async def call(index: int) -> None:
        ut.set_current_tree(trees[index])
        with ut.usage_scope(f"task-{index}"):
            handler.on_chat_model_start(
                {}, [], run_id=index, metadata={"nova_provider": "commandcode"}
            )
        ut.set_current_tree(None)
        await asyncio.sleep(0)
        if index:
            handler.on_llm_error(asyncio.CancelledError(), run_id=index)
        else:
            handler.on_llm_new_token("x", run_id=index)
            handler.on_llm_end(response({"input_tokens": 12}), run_id=index)

    await asyncio.gather(call(0), call(1))
    assert trees[0].node_for(("task-0",)).input_tokens == 12
    assert trees[1].node_for(("task-1",)).unavailable_calls == 1
    assert trees[0].requests[0]["first_token_ms"] is not None
    assert trees[0].requests[0]["provider"] == "commandcode"
    assert not handler._runs
    assert not handler._model_names


def test_prefix_hashes_distinguish_settings_system_tools_and_history():
    model = FakeListChatModel(responses=["ok"])
    request = ModelRequest(
        model=model,
        messages=[HumanMessage("private message")],
        system_message=SystemMessage("private system"),
        tools=[],
        state={},
        model_settings={"temperature": 0},
    )
    first = ContextSnapshot.capture(request)
    state = {"_context_snapshot": first.diagnostics()}
    unchanged = ContextSnapshot.capture(request.override(state=state))
    assert unchanged.prefix_changes == ()
    changed = ContextSnapshot.capture(
        request.override(
            state=state,
            system_message=SystemMessage("updated system"),
            model_settings={"temperature": 1},
        )
    )
    assert set(changed.prefix_changes) == {"system", "settings"}
    diagnostic = str(changed.diagnostics())
    assert "private" not in diagnostic
    assert "updated system" not in diagnostic
    assert changed.diagnostics()["cache_miss_cause"] == "unknown"
