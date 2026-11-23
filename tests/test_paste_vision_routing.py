"""The paste → model-request path for images, in both routing modes.

A pasted image arrives as a `[image-2]` marker in the text plus a tracked
image that `_prepare_input` turns into an image content block. The composer
bug report: a multimodal-capable model narrated "[image-2] arrived as a
placeholder only" — the marker text reached the model but the image block did
not. These tests pin both routing modes:

* multimodal main model → the image content block survives to the request;
* text-only main model → the block is stripped (the marker text remains) and
  the VISION fallback is what the failure placeholder points at.
"""

from __future__ import annotations

import base64

import pytest
from langchain_core.messages import HumanMessage

from novacode_cli.bootstrap.vision_router import VisionCaptionMiddleware
from novacode_cli.image_utils import ImageData


def _paste() -> tuple[str, ImageData]:
    png = base64.b64encode(bytes.fromhex("89504e470d0a1a0a")).decode()
    image = ImageData(base64_data=png, format="png", placeholder="[image-2]")
    return "repaint the hero using [image-2]", image


def _request_with_paste():
    from types import SimpleNamespace

    text, image = _paste()
    content = [
        {"type": "text", "text": text},
        image.to_message_content(),
    ]
    messages = [HumanMessage(content=content)]

    def handler(request):
        return request

    return SimpleNamespace(
        messages=messages,
        system_prompt="BASE",
        model=None,
        override=lambda **kw: SimpleNamespace(
            messages=kw.get("messages", messages),
            system_prompt=kw.get("system_prompt", "BASE"),
            model=None,
            tools=kw.get("tools"),
            model_settings=kw.get("model_settings"),
        ),
    ), handler


def _images_in(request) -> int:
    count = 0
    for message in request.messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            count += sum(
                1
                for block in content
                if isinstance(block, dict) and block.get("type") in ("image", "image_url")
            )
    return count


def test_multimodal_main_model_receives_the_pasted_image() -> None:
    """A detected-multimodal model gets the image block — not just the marker."""
    request, handler = _request_with_paste()
    mw = VisionCaptionMiddleware(main_model_supports_images=True)
    out = mw.wrap_model_call(request, handler)
    assert _images_in(out) == 1, "the image must pass through to a multimodal model"
    text = out.messages[0].content[0]["text"]
    assert "[image-2]" in text


def test_text_only_main_model_strips_the_image_but_keeps_the_marker() -> None:
    """A text-only model never receives image bytes; the marker text stays."""
    request, handler = _request_with_paste()
    mw = VisionCaptionMiddleware(main_model_supports_images=False)
    out = mw.wrap_model_call(request, handler)
    assert _images_in(out) == 0, "image bytes must not reach a text-only model"
    text = out.messages[0].content[0]["text"]
    assert "[image-2]" in text, "the marker stays so the model can reference the image"


def test_vision_failure_placeholder_demands_immediate_resolution() -> None:
    """The failure text must not let the model defer image-dependent work."""
    from novacode_cli.bootstrap.vision_router import caption_images, is_vision_failure

    text, image = _paste()

    async def run() -> str:
        return await caption_images([image.to_data_url()], text)

    import asyncio

    caption = asyncio.run(run())
    assert is_vision_failure(caption)
    low = caption.lower()
    assert "/vision on" in low
    assert "right now" in low or "do not defer" in low, (
        "the placeholder must demand immediate resolution, not 'later'"
    )
