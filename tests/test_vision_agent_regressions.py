"""Image transport must follow the receiving agent, preserving graph state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.bootstrap import vision_router as vr


@pytest.mark.parametrize("child_images", [True, False])
async def test_subagent_provider_receives_correct_image_payload(*, child_images: bool):
    import json

    import httpx
    from langchain_openai import ChatOpenAI

    from novacode_cli.agents.core_agent import _harden_subagent_specs, _original_create_sub_agent

    payloads = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "custom",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Done"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(
            model="custom",
            api_key="test",
            http_async_client=client,
            profile={"image_inputs": child_images},
        )
        spec = _harden_subagent_specs(
            [
                {
                    "name": "image-reviewer",
                    "description": "Inspect images",
                    "system_prompt": "Review",
                    "tools": [],
                    "model": model,
                }
            ],
            main_model_supports_images=not child_images,
        )[0]
        graph = _original_create_sub_agent(spec)
        message = HumanMessage(
            content=[
                {"type": "text", "text": "Inspect this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ]
        )
        result = await graph.ainvoke({"messages": [message]})
    assert payloads
    sent = json.dumps(payloads[-1]["messages"])
    assert ("data:image/png;base64,abc" in sent) is child_images
    if not child_images:
        assert "call VISION" in sent or "Call VISION" in sent
        assert any(t["function"]["name"] == "VISION" for t in payloads[-1]["tools"])
    assert vr._collect_image_urls(result["messages"][0]) == ["data:image/png;base64,abc"]


@pytest.mark.parametrize(("main_images", "child_images"), [(False, True), (True, False)])
def test_subagent_uses_own_model_capability(*, main_images: bool, child_images: bool):
    from novacode_cli.agents.core_agent import _harden_subagent_specs

    model = SimpleNamespace(profile={"image_inputs": child_images})
    original = {"name": "reviewer", "model": model, "tools": []}
    spec = _harden_subagent_specs([original], main_model_supports_images=main_images)[0]
    middleware = next(m for m in spec["middleware"] if isinstance(m, vr.VisionCaptionMiddleware))
    assert middleware.main_model_supports_images is child_images
    if not child_images:
        assert sum(t.name == "VISION" for t in spec["tools"]) == 1
    assert original["tools"] == []


def test_standard_url_image_available_to_vision():
    message = HumanMessage(content=[{"type": "image", "url": "https://example.com/ui.png"}])
    assert vr._collect_image_urls(message) == ["https://example.com/ui.png"]


def test_stripping_preserves_message_identity_and_tool_status():
    message = ToolMessage(
        content=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
        id="result-1",
        tool_call_id="read-1",
        status="error",
    )
    stripped = vr._strip_image_content(message)
    assert stripped.id == message.id
    assert stripped.status == "error"
    assert vr._collect_image_urls(message)
    assert not vr._collect_image_urls(stripped)


async def test_structured_caption_uses_only_text(monkeypatch: pytest.MonkeyPatch):
    vr.reset_vision_model_cache()
    model = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=AIMessage(
                content=[
                    {"type": "reasoning", "reasoning": "private reasoning"},
                    {"type": "text", "text": "A red button."},
                ]
            )
        )
    )
    monkeypatch.setattr(vr, "get_vision_model", lambda: model)
    assert await vr.caption_images(["https://example.com/ui.png"]) == "A red button."


async def test_initialization_failure_recovers(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config import model_create
    from novacode_cli.config.nova_config import NovaConfig

    vr.reset_vision_model_cache()
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="Recovered")))
    attempts = iter([None, model])
    monkeypatch.setattr(model_create, "create_model_from_config", lambda *_args: next(attempts))
    monkeypatch.setattr(
        NovaConfig, "get_vision_model_config", lambda _self: {"provider": "test", "model": "vision"}
    )
    try:
        assert vr.is_vision_failure(await vr.caption_images(["image"]))
        for _ in range(vr._VISION_FAILURE_COOLDOWN):
            assert vr.is_vision_failure(await vr.caption_images(["image"]))
        assert await vr.caption_images(["image"]) == "Recovered"
    finally:
        vr.reset_vision_model_cache()
