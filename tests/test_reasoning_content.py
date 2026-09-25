"""Thinking models get their reasoning_content echoed back.

DeepSeek/GLM-style models return their chain of thought in `reasoning_content`
and then require it sent back with that assistant message on the next turn:

    400 invalid_request_error — "The `reasoning_content` in the thinking mode
    must be passed back to the API."

langchain_openai's _convert_message_to_dict emits only role/content/tool_calls,
so the field is parsed in and silently dropped on the way out — which breaks the
second request of every conversation, and for an agent a tool call IS a second
request.

The safety property is the other half: the field must only ever be re-attached
when the model actually produced it, so strict endpoints that have never heard
of it see exactly the payload they see today.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.utils.backend_patches import apply_openai_reasoning_content_patch


def _convert(message):
    """Serialize through the (patched) OpenAI message converter."""
    apply_openai_reasoning_content_patch()
    from langchain_openai.chat_models.base import _convert_message_to_dict

    return _convert_message_to_dict(message)


def test_reasoning_content_survives_serialization():
    """Without this the next request 400s on every thinking model."""
    out = _convert(
        AIMessage(content="4", additional_kwargs={"reasoning_content": "2+2 is 4"})
    )
    assert out["reasoning_content"] == "2+2 is 4"
    assert out["content"] == "4"


def test_it_survives_alongside_tool_calls():
    """The agent path: a tool call is the second turn, so this is the common case."""
    out = _convert(
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "I should check the weather"},
            tool_calls=[
                {"name": "get_weather", "args": {}, "id": "call_1", "type": "tool_call"}
            ],
        )
    )
    assert out["reasoning_content"] == "I should check the weather"
    assert out["tool_calls"], "tool calls must still be serialized"


def test_a_reasoning_field_is_also_carried():
    """Some gateways name it `reasoning` rather than `reasoning_content`."""
    out = _convert(AIMessage(content="hi", additional_kwargs={"reasoning": "because"}))
    assert out["reasoning"] == "because"


# ── The safety half: never invent the field ─────────────────────────────────


def test_a_normal_message_is_untouched():
    """OpenAI has never heard of reasoning_content; it must not be sent one."""
    out = _convert(AIMessage(content="hello", additional_kwargs={"refusal": None}))
    assert "reasoning_content" not in out
    assert "reasoning" not in out
    assert sorted(out) == ["content", "role"]


def test_empty_reasoning_is_not_sent():
    """'' means the model returned no thought — not the same as sending one."""
    for empty in ("", None):
        out = _convert(
            AIMessage(content="hi", additional_kwargs={"reasoning_content": empty})
        )
        assert "reasoning_content" not in out


def test_human_and_tool_messages_are_unaffected():
    assert sorted(_convert(HumanMessage("yo"))) == ["content", "role"]
    tool = _convert(ToolMessage(content="42", tool_call_id="call_1"))
    assert "reasoning_content" not in tool


def test_the_patch_is_idempotent():
    """Applied on every model build; must not stack wrappers."""
    for _ in range(5):
        apply_openai_reasoning_content_patch()
    out = _convert(
        AIMessage(content="x", additional_kwargs={"reasoning_content": "once"})
    )
    assert out["reasoning_content"] == "once"


def test_building_an_openai_compatible_model_applies_the_patch(monkeypatch):
    """It has to be live before the first request, not on some later path."""
    import novacode_cli.utils.backend_patches as bp

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(bp, "_reasoning_content_patched", False)
    called: list[bool] = []
    real = bp.apply_openai_reasoning_content_patch
    monkeypatch.setattr(
        bp,
        "apply_openai_reasoning_content_patch",
        lambda: (called.append(True), real())[1],
    )

    from novacode_cli.config.model_create import build_chat_model

    build_chat_model("openai", "gpt-4o")
    assert called, "model built without the reasoning_content patch applied"


# ── The inbound half: the field has to arrive before it can be echoed ────────
#
# Every test above hands the converter an AIMessage that ALREADY carries
# reasoning_content in additional_kwargs — so they all passed while the 400 kept
# happening in production. langchain_openai drops the field on the way IN too
# ("non-standard response fields are not extracted or preserved"), leaving the
# outbound patch with nothing to echo.


def _chunk(delta: dict):
    from langchain_core.messages import AIMessageChunk
    from langchain_openai.chat_models.base import _convert_delta_to_message_chunk

    apply_openai_reasoning_content_patch()
    return _convert_delta_to_message_chunk(delta, AIMessageChunk)


def test_streaming_deltas_keep_the_reasoning():
    apply_openai_reasoning_content_patch()
    chunk = _chunk({"role": "assistant", "content": "", "reasoning_content": "hmm"})
    assert chunk.additional_kwargs.get("reasoning_content") == "hmm"


def test_a_non_streaming_response_keeps_the_reasoning():
    apply_openai_reasoning_content_patch()
    from langchain_openai.chat_models.base import _convert_dict_to_message

    msg = _convert_dict_to_message(
        {"role": "assistant", "content": "4", "reasoning_content": "2+2"}
    )
    assert msg.additional_kwargs.get("reasoning_content") == "2+2"


def test_the_full_stream_round_trip():
    """What actually happens on a turn: fragments in, one echoed field out."""
    apply_openai_reasoning_content_patch()
    merged = _chunk({"role": "assistant", "content": "", "reasoning_content": "Let me "})
    for frag in ("check the ", "file. "):
        merged = merged + _chunk({"content": "", "reasoning_content": frag})
    merged = merged + _chunk({"content": "Done."})

    out = _convert(merged)
    assert out["reasoning_content"] == "Let me check the file. ", "fragments must merge"
    assert out["content"] == "Done."


def test_a_stream_without_reasoning_stays_clean():
    apply_openai_reasoning_content_patch()
    out = _convert(_chunk({"role": "assistant", "content": "hi"}))
    assert "reasoning_content" not in out


def test_echoed_reasoning_is_counted_as_context():
    """It rides on every later request, so ctx% has to see it."""
    from novacode_cli.context._analysis import build_context_breakdown

    plain = build_context_breakdown(
        [AIMessage(content="ok")], "claude-opus-5", use_dynamic=False
    )
    thinking = build_context_breakdown(
        [AIMessage(content="ok", additional_kwargs={"reasoning_content": "t" * 8_000})],
        "claude-opus-5",
        use_dynamic=False,
    )
    assert thinking.assistant_message_tokens - plain.assistant_message_tokens == 2_000
