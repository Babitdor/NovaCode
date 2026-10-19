"""Offline contracts for lossless request observations and usage reporting."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.tracking import usage_tree as ut


def read_pair(call_id: str, text: str, path: str = "a.py", name: str = "read_file"):
    return [
        AIMessage("", tool_calls=[{"id": call_id, "name": name, "args": {"file_path": path}}]),
        ToolMessage(text, tool_call_id=call_id, name=name),
    ]


def test_duplicate_read_preserves_fresh_result_and_original_history():
    from novacode_cli.context.observations import compact_observations

    text = "original\n" * 1000
    changed = "changed\n" * 1000
    messages = [
        HumanMessage("Keep the public API."),
        *read_pair("a", text),
        *read_pair("b", text),
        *read_pair("c", changed),
        *read_pair("d", changed),
    ]
    original = [m.model_dump() for m in messages]
    result = compact_observations(messages)
    assert result[2].content == text
    assert result[6].content == changed
    assert "Identical" in result[4].content
    assert "Identical" in result[8].content
    assert "message 3" in result[4].content
    assert "message 7" in result[8].content
    assert [m.model_dump() for m in messages] == original
    assert [m.tool_call_id for m in result if isinstance(m, ToolMessage)] == ["a", "b", "c", "d"]
    assert compact_observations(result) == result
    # Resume/compaction drops the original: the retained result must be whole again.
    assert compact_observations(messages[3:])[1].content == text


@pytest.mark.parametrize("name", ["execute", "edit_file", "write_file", "fetch_url"])
def test_side_effects_and_other_tools_are_never_deduplicated(name: str):
    from novacode_cli.context.observations import compact_observations

    messages = [*read_pair("a", "x" * 8000, name=name), *read_pair("b", "x" * 8000, name=name)]
    assert compact_observations(messages) == messages


def test_reads_require_same_arguments_success_and_text_content():
    from novacode_cli.context.observations import compact_observations

    messages = [*read_pair("a", "x" * 8000), *read_pair("b", "x" * 8000, path="b.py")]
    assert compact_observations(messages) == messages
    messages = [*read_pair("a", "x" * 8000), *read_pair("a", "x" * 8000)]
    messages[-1].status = "error"
    assert compact_observations(messages) == messages
    messages[-1].content = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
    ]
    assert compact_observations(messages) == messages
    assert compact_observations(read_pair("a", "x" * 8000))[1].content == "x" * 8000


def test_total_cost_uses_each_model_and_survives_persistence():
    tree = ut.UsageTree("offline")
    tree.record(("main",), "gpt-5", {"input_tokens": 1_000_000})
    tree.record(("main",), "gpt-5-mini", {"input_tokens": 1_000_000})
    tree.record(("verification",), "gpt-5", {"output_tokens": 1_000_000})
    restored = ut.UsageTree.from_dict(tree.to_dict())
    assert "$1.50" in restored.render()
    assert "$11.50" in restored.render().splitlines()[-1]
    assert restored.root.totals()["input_tokens"] == 2_000_000


def test_unknown_usage_and_prices_are_unavailable():
    tree = ut.UsageTree("offline")
    tree.record(("main",), "unpriced", {"input_tokens": 200})
    assert "unknown" in tree.render().splitlines()[-1]
    assert ut.estimate_cost(100, 20, "unpriced") is None
    handler = ut.UsageCallbackHandler()
    ut.set_current_tree(tree)
    try:
        with ut.usage_scope("retry"):
            handler.on_llm_start({"kwargs": {"model": "gpt-5"}}, [], run_id="error")
            handler.on_llm_error(RuntimeError("offline"), run_id="error")
        assert tree.node_for(("retry",)).unavailable_calls == 1
    finally:
        ut.set_current_tree(None)


@pytest.mark.parametrize("model", ["gpt-5-future", "gemini-2.5-flash", "deepseek-unknown"])
def test_unpriced_model_variants_do_not_inherit_a_family_price(model: str):
    assert ut.estimate_cost(1_000_000, 1_000_000, model) is None


@pytest.mark.parametrize("model", ["openai/gpt-5-mini", "gpt-5-mini-2025-08-07"])
def test_known_model_namespaces_and_dated_snapshots_keep_the_bundled_rate(model: str):
    assert ut.estimate_cost(1_000_000, 0, model) == pytest.approx(0.25)


def test_anthropic_raw_cache_is_normalized_once():
    response = SimpleNamespace(
        generations=[],
        llm_output={
            "usage": {
                "input_tokens": 7,
                "output_tokens": 3,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
            }
        },
    )
    assert ut._extract_usage(response)["input_tokens"] == 10
    normalized = SimpleNamespace(
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 3,
                            "input_token_details": {"cache_read": 2, "cache_creation": 1},
                        }
                    )
                )
            ]
        ]
    )
    assert ut._extract_usage(normalized)["input_tokens"] == 10


def test_legacy_usage_record_still_has_a_cost():
    node = ut.UsageNode.from_dict(
        {"scope": "main", "model": "gpt-5", "calls": 1, "input_tokens": 1_000_000}
    )
    tree = ut.UsageTree("old")
    tree.root.children.append(node)
    assert "$1.25" in tree.render().splitlines()[-1]


def test_serialized_usage_does_not_follow_later_accumulation():
    tree = ut.UsageTree("snapshot")
    tree.record(("main",), "gpt-5", {"input_tokens": 1_000_000})
    saved = tree.to_dict()
    tree.record(("main",), "gpt-5", {"input_tokens": 1_000_000})
    assert "$1.25" in ut.UsageTree.from_dict(saved).render().splitlines()[-1]
    assert "$2.50" in tree.render().splitlines()[-1]


def test_image_estimates_do_not_depend_on_base64_length():
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.context_compaction import _request_size_tokens
    from novacode_cli.context.snapshot import ContextSnapshot

    small = HumanMessage(content=[{"type": "input_image", "image_url": "data:image/png;base64,a"}])
    large = HumanMessage(
        content=[{"type": "input_image", "image_url": "data:image/png;base64," + "a" * 12000}]
    )
    assert _request_size_tokens([small]) == _request_size_tokens([large])
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]), messages=[large], tools=[], state={}
    )
    diagnostic = ContextSnapshot.capture(request).diagnostics()
    assert diagnostic["estimated_multimodal_tokens"] == 85
    assert diagnostic["estimated_text_tokens"] < 100
    assert diagnostic["image_transport_chars"] == 12000
    assert diagnostic["estimated_schema_tokens"] == 0


@pytest.mark.parametrize("window", [4_000, 128_000, 1_000_000])
async def test_dispatch_contract_across_budgets_and_concurrent_sessions(window: int):
    import asyncio

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware
    from novacode_cli.context.snapshot import (
        ContextSnapshotMiddleware,
        ProtectedContextOverflowError,
    )

    middleware = ContextSnapshotMiddleware()

    async def dispatch(session: str) -> None:
        content = f"private contents for {session}\n" * 150
        history = [
            HumanMessage(f"Approved plan for {session}: retain Unicode é. Pending: verify."),
            *read_pair("reused", content),
            *read_pair("reused", content),
        ]
        request = ModelRequest(
            model=FakeListChatModel(responses=["ok"]),
            messages=history,
            tools=[],
            state={"messages": history},
            model_settings={"max_tokens": 100},
        )
        original = [m.model_dump() for m in history]
        plan = ContextCompactionMiddleware(context_window=window)._plan(request)
        assert plan.cutoff == 0

        async def handler(actual: ModelRequest) -> ModelResponse:
            await asyncio.sleep(0)
            assert actual.messages[0] == history[0]
            assert actual.messages[2].content == content
            assert "message 3" in actual.messages[4].content
            assert [m.tool_call_id for m in actual.messages if isinstance(m, ToolMessage)] == [
                "reused",
                "reused",
            ]
            return ModelResponse(result=[AIMessage("verified")])

        response = await middleware.awrap_model_call(request, handler)
        assert [m.model_dump() for m in history] == original
        assert response.command.update["_context_snapshot"]["output_reservation"] == 100
        oversized = request.override(messages=[HumanMessage("protected " * window)])
        with pytest.raises(ProtectedContextOverflowError):
            ContextCompactionMiddleware(context_window=window)._plan(oversized)
        # Restart with the original checkpoint, or keep only its latest complete pair.
        await middleware.awrap_model_call(request, handler)
        shortened = request.override(messages=history[3:])

        async def resumed(actual: ModelRequest) -> ModelResponse:
            assert actual.messages[1].content == content
            return ModelResponse(result=[AIMessage("continued")])

        await middleware.awrap_model_call(shortened, resumed)

    await asyncio.gather(dispatch("session-a"), dispatch("session-b"))


def test_nested_image_transport_is_separate_from_token_estimate(monkeypatch):
    from novacode_cli.agents import context_compaction
    from novacode_cli.context.estimates import estimate_content

    monkeypatch.setattr(context_compaction, "MAX_REQUEST_CHARS", 30_000)

    def result(size: int) -> ToolMessage:
        return ToolMessage(
            content=[
                {
                    "type": "tool_result",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "data": "a" * size}},
                        {"type": "text", "text": "Keep this description."},
                    ],
                }
            ],
            tool_call_id="frame",
        )

    small, large = estimate_content([result(10)]), estimate_content([result(10000)])
    assert small.total == large.total
    assert small.image_tokens == large.image_tokens == 85
    assert large.image_transport_chars == 10000
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware

    messages = [HumanMessage("Keep context."), *read_pair("small", "small")]
    for index in range(3):
        messages.extend(read_pair(f"nested-{index}", ""))
        messages[-1].content = result(12000).content
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=messages,
        state={"messages": messages},
        tools=[],
    )
    plan = ContextCompactionMiddleware(context_window=4000)._plan(request)
    assert plan.image_transport_chars == 36000
    assert plan.raw_tokens < 4000
    assert plan.cutoff > 0


def test_combined_text_and_image_body_still_triggers_transport_guard(monkeypatch):
    from novacode_cli.agents import context_compaction
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware, limits_for

    window = 4000
    limits = limits_for(window)
    monkeypatch.setattr(context_compaction, "MAX_REQUEST_CHARS", limits.trigger * 4)
    text = "x" * int(limits.trigger * 4 * 0.65)
    image = "a" * int(limits.trigger * 4 * 0.6)
    messages = [
        HumanMessage("Keep constraints."),
        *read_pair("text", text),
        *read_pair("image", ""),
    ]
    messages[-1].content = [{"type": "image", "base64": image}]
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=messages,
        state={"messages": messages},
        tools=[],
    )
    plan = ContextCompactionMiddleware(context_window=window)._plan(request)
    assert plan.raw_tokens < limits.trigger
    assert plan.image_transport_chars < limits.trigger * 4
    assert plan.cutoff > 0


@pytest.mark.parametrize("image", [False, True])
def test_compaction_requires_real_savings_after_sources_move_to_the_tail(*, image: bool):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.context_compaction import ContextCompactionMiddleware, limits_for

    window = 4000
    limits = limits_for(window)
    messages = [
        HumanMessage("Keep the API."),
        *read_pair("first", "tiny" if image else "x" * limits.trigger * 4),
        *read_pair("last", "x" * limits.trigger * 4),
    ]
    if image:
        messages[-1].content = [{"type": "image", "base64": "a" * limits.trigger * 8}]
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=messages,
        state={"messages": messages},
        tools=[],
    )
    middleware = ContextCompactionMiddleware(context_window=window)
    # Keeping the full repeated source, or an oversized newest frame, leaves
    # too little removable input to justify a summary and another restoration.
    assert middleware._plan(request).cutoff == 0
    assert middleware._plan(request).cutoff == 0
