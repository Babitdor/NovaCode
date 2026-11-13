"""Provider cache formats, including the streaming SDK boundary used by Nova."""

from types import SimpleNamespace

import pytest

from novacode_cli.tracking import usage_tree as ut


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 100},
            },
            {
                "input_tokens": 1000,
                "output_tokens": 10,
                "cache_read_tokens": 800,
                "cache_creation_tokens": 100,
                "uncached_tokens": 100,
            },
        ),
        (
            {
                "input_tokens": 1000,
                "output_tokens": 10,
                "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 100},
            },
            {
                "input_tokens": 1000,
                "output_tokens": 10,
                "cache_read_tokens": 800,
                "cache_creation_tokens": 100,
                "uncached_tokens": 100,
            },
        ),
        (
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 100,
            },
            {
                "input_tokens": 1000,
                "output_tokens": 10,
                "cache_read_tokens": 800,
                "cache_creation_tokens": 100,
                "uncached_tokens": 100,
            },
        ),
        (
            {
                "inputTokens": 100,
                "outputTokens": 10,
                "cacheReadInputTokens": 800,
                "cacheWriteInputTokens": 100,
            },
            {
                "input_tokens": 1000,
                "output_tokens": 10,
                "cache_read_tokens": 800,
                "cache_creation_tokens": 100,
                "uncached_tokens": 100,
            },
        ),
        (
            {"promptTokenCount": 1000, "candidatesTokenCount": 10, "cachedContentTokenCount": 800},
            {"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 800},
        ),
        (
            {"prompt_eval_count": 1000, "eval_count": 10},
            {"input_tokens": 1000, "output_tokens": 10},
        ),
        (
            {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
            },
            {
                "input_tokens": 1000,
                "output_tokens": 10,
                "cache_read_tokens": 800,
                "uncached_tokens": 200,
            },
        ),
    ],
)
def test_raw_provider_usage(raw: dict, expected: dict):
    result = SimpleNamespace(generations=[], llm_output={"usage": raw})
    assert ut._extract_usage(result) == expected


@pytest.mark.parametrize("streaming", [True, False])
async def test_factory_preserves_gateway_cache_writes_through_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    streaming: bool,
):
    import json

    import httpx
    from langchain_openai import ChatOpenAI

    from novacode_cli.config import model_create

    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 1,
        "total_tokens": 1001,
        "prompt_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 700},
    }

    async def respond(request: httpx.Request) -> httpx.Response:
        base = {"id": "cache-test", "model": "custom", "created": 0}
        if json.loads(request.content).get("stream"):
            chunks = [
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "OK"},
                            "finish_reason": None,
                        }
                    ],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {**base, "choices": [], "usage": usage},
            ]
            body = (
                "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                **base,
                "usage": usage,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "OK"},
                    }
                ],
            },
        )

    tree = ut.UsageTree("gateway")
    ut.set_current_tree(tree)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = ChatOpenAI(
                model="custom", api_key="test", http_async_client=client, stream_usage=True
            )
            monkeypatch.setattr(model_create, "_build_chat_model", lambda *_a, **_kw: model)
            model = model_create.build_chat_model("openrouter", "custom")
            if streaming:
                async for _chunk in model.astream("Synthetic probe"):
                    pass
            else:
                await model.ainvoke("Synthetic probe")
        assert tree.root.totals()["cache_read_tokens"] == 200
        assert tree.root.totals()["cache_creation_tokens"] == 700
        assert tree.root.totals()["input_tokens"] == 1000
    finally:
        ut.set_current_tree(None)


@pytest.mark.parametrize("responses", [False, True])
def test_sdk_cache_patch_preserves_zero_absence_and_is_idempotent(responses):
    from langchain_openai.chat_models import base

    from novacode_cli.utils.backend_patches import apply_openai_cache_usage_patch

    apply_openai_cache_usage_patch()
    name = "_create_usage_metadata_responses" if responses else "_create_usage_metadata"
    normalize = getattr(base, name)
    apply_openai_cache_usage_patch()
    assert getattr(base, name) is normalize
    assert normalize({})["input_token_details"] == {}
    detail_key = "input_tokens_details" if responses else "prompt_tokens_details"
    result = normalize(
        {detail_key: {"cached_tokens": 0, "cache_write_tokens": 0}, "prompt_cache_hit_tokens": 900}
    )
    assert result["input_token_details"] == {"cache_read": 0, "cache_creation": 0}
    assert normalize({"prompt_cache_hit_tokens": 700})["input_token_details"] == {
        "cache_read": 700,
    }


def test_write_only_reporting_keeps_known_counts_and_flags_partial_observations():
    tree = ut.UsageTree("writes-only")
    tree.record(("main",), "custom", {"input_tokens": 1000, "cache_creation_tokens": 700})
    tree.record(("main",), "custom", {"input_tokens": 1000})
    tree.record_request({"model": "custom", "usage": {"input_tokens": 1000}})
    report = ut.UsageTree.from_dict(tree.to_dict()).render()
    assert "Cache reuse: unavailable; 700 written (partial reporting)" in report
    assert "cache read unavailable" in report
