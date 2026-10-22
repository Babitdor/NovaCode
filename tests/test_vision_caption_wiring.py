"""Clipboard images stay in graph state until VISION is explicitly called."""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage


def _image_data():
    from PIL import Image

    from novacode_cli.image_utils import ImageData

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 120, 220)).save(buf, format="PNG")
    return ImageData(
        base64_data=base64.b64encode(buf.getvalue()).decode(),
        format="png",
        placeholder="[image]",
    )


def test_input_preparation_defers_auxiliary_call(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.core import input_preparation
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    tracker.add_image(_image_data())
    caption = AsyncMock(return_value="a small blue square")
    monkeypatch.setattr("novacode_cli.bootstrap.vision_router.caption_images", caption)

    out = asyncio.run(
        input_preparation.prepare_input_content(
            "describe this", image_tracker=tracker, skip_file_mentions=True
        )
    )

    assert isinstance(out, list)
    assert out[0]["text"] == "describe this"
    assert out[1]["type"] == "image_url"
    assert isinstance(out[1]["image_url"]["url"], str)
    caption.assert_not_awaited()


async def test_vision_tool_reads_latest_image_only_when_invoked(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.bootstrap import vision_router as vr

    caption = AsyncMock(return_value="a small blue square")
    monkeypatch.setattr(vr, "caption_images", caption)
    image_url = _image_data().to_data_url()
    tool = vr.build_vision_tool()
    result = await tool.coroutine(
        runtime=SimpleNamespace(
            state={
                "messages": [
                    HumanMessage(
                        content=[
                            {"type": "text", "text": "describe this"},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ]
                    )
                ]
            }
        )
    )

    assert result == "a small blue square"
    caption.assert_awaited_once_with([image_url], "describe this")
    assert tool.name == "VISION"


async def test_vision_tool_does_not_reuse_an_image_from_an_earlier_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    from novacode_cli.bootstrap import vision_router as vr

    caption = AsyncMock(return_value="description")
    monkeypatch.setattr(vr, "caption_images", caption)
    old_image = _image_data().to_data_url()
    tool = vr.build_vision_tool()
    result = await tool.coroutine(
        runtime=SimpleNamespace(
            state={
                "messages": [
                    HumanMessage(
                        content=[{"type": "image_url", "image_url": {"url": old_image}}]
                    ),
                    HumanMessage(content="What is the weather today?"),
                ]
            }
        )
    )

    assert "could not find" in result
    caption.assert_not_awaited()


def test_text_only_model_request_strips_image_but_preserves_vision_hint():
    from novacode_cli.bootstrap.vision_router import _collect_image_urls, _strip_image_content

    message = HumanMessage(
        content=[
            {"type": "text", "text": "fix this"},
            {"type": "image_url", "image_url": {"url": _image_data().to_data_url()}},
        ]
    )
    forwarded = _strip_image_content(message)

    assert _collect_image_urls(forwarded) == []
    assert "fix this" in forwarded.text
    assert "VISION" in forwarded.text


def test_vision_tool_returns_visible_failure_reason(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.bootstrap import vision_router as vr

    model = AsyncMock()
    model.ainvoke.side_effect = TimeoutError
    monkeypatch.setattr(vr, "get_vision_model", lambda: model)
    vr._vision_cooldown_remaining = 0
    result = asyncio.run(vr.caption_images([_image_data().to_data_url()], "inspect"))

    assert "timed out" in result
    assert vr.is_vision_failure(result)
