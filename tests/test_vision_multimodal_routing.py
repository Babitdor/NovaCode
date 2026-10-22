"""Pasted images flow directly or stay available for an on-demand VISION call."""

from __future__ import annotations

import asyncio
import base64
import io


def _tracker():
    from PIL import Image

    from novacode_cli.image_utils import ImageData
    from novacode_cli.input_utils import ImageTracker

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 90, 200)).save(buf, format="PNG")
    tracker = ImageTracker()
    tracker.add_image(
        ImageData(
            base64_data=base64.b64encode(buf.getvalue()).decode(),
            format="png",
            placeholder="[image]",
        )
    )
    return tracker


def _prepare(monkeypatch, *, multimodal, caption_fn):
    """Run the real path, stubbing only the capability answer and the model."""
    from novacode_cli.bootstrap import vision_router
    from novacode_cli.core import input_preparation

    monkeypatch.setattr(input_preparation, "_main_model_can_see_images", lambda: multimodal)
    monkeypatch.setattr(vision_router, "caption_images", caption_fn)
    return asyncio.run(
        input_preparation.prepare_input_content("what is this?", image_tracker=_tracker())
    )


async def _real_caption(image_urls, *args, **kwargs):
    return "a PDF viewer showing page 9"


# ── the main fix: a multimodal model gets the image, not a caption ───────────


def test_multimodal_main_model_receives_image_blocks(monkeypatch):
    """The reported case: OpenCode/deepseek-v4.1-flash can see images."""
    out = _prepare(monkeypatch, multimodal=True, caption_fn=_real_caption)
    assert isinstance(out, list), "a multimodal model must get image blocks, not a caption"
    assert out[0]["type"] == "text"
    assert out[0]["text"] == "what is this?"
    assert out[1]["type"] == "image_url"
    assert out[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_multimodal_path_does_not_call_the_vision_model(monkeypatch):
    """Captioning must be skipped entirely — that is the whole point."""
    calls: list = []

    async def spy(image_urls, *args, **kwargs):
        calls.append(image_urls)
        return "unused"

    _prepare(monkeypatch, multimodal=True, caption_fn=spy)
    assert calls == [], "the auxiliary vision model was called for a multimodal main model"


# ── a text-only model still captions ────────────────────────────────────────


def test_text_only_model_defers_inspection_until_the_vision_tool_is_called(monkeypatch):
    calls: list = []

    async def spy(image_urls, *args, **kwargs):
        calls.append(image_urls)
        return "a screenshot description"

    out = _prepare(monkeypatch, multimodal=False, caption_fn=spy)
    assert isinstance(out, list)
    assert out[0]["text"] == "what is this?"
    assert out[1]["type"] == "image_url"
    assert calls == [], "image inspection must wait for an explicit VISION tool call"


# ── the capability answer has one source of truth ───────────────────────────


def test_the_middleware_and_the_ingest_path_share_one_answer():
    """If these disagree, images are captioned for a model that can read them."""
    from novacode_cli.agents.core_agent import _resolve_main_model_multimodal
    from novacode_cli.config.model_capabilities import resolve_main_model_multimodal

    assert _resolve_main_model_multimodal(None) == resolve_main_model_multimodal(None)


def test_the_openly_configured_opencode_model_is_detected_as_multimodal():
    """Pins the actual reported configuration (from Nova.config.json)."""
    from novacode_cli.config.model_capabilities import model_supports_images

    assert model_supports_images("opencode", "deepseek-v4.1-flash") is True


def test_an_unknown_model_is_not_assumed_multimodal():
    """A wrong `True` here would send images to a model that rejects them."""
    from novacode_cli.config.model_capabilities import model_supports_images

    assert model_supports_images("opencode", "some-text-only-model") is False


def test_the_user_override_still_wins():
    from novacode_cli.config.model_capabilities import model_supports_images

    # An unlisted multimodal model can be declared without a code change.
    assert model_supports_images("opencode", "text-only-model", override=True) is True
    assert model_supports_images("openai", "gpt-4o", override=False) is False


def test_is_vision_failure_distinguishes_placeholders_from_descriptions():
    from novacode_cli.bootstrap.vision_router import (
        _VISION_EMPTY,
        _VISION_FAILED,
        _VISION_UNAVAILABLE,
        is_vision_failure,
    )

    assert is_vision_failure(_VISION_FAILED)
    assert is_vision_failure(_VISION_UNAVAILABLE)
    assert is_vision_failure(_VISION_EMPTY)
    # A real description is not a failure, even if it mentions the word.
    assert not is_vision_failure("A screenshot of a vision model dashboard")
    assert not is_vision_failure("")


def test_a_multimodal_verdict_is_written_onto_the_model_profile() -> None:
    """deepagents strips images when the PROFILE says the model can't take them."""
    from langchain_openai import ChatOpenAI

    from novacode_cli.agents.core_agent import _declare_image_support

    model = ChatOpenAI(model="gpt-4o", api_key="test-key")
    model.profile = {"max_input_tokens": 128_000, "image_inputs": False}

    _declare_image_support(model)

    assert model.profile["image_inputs"] is True
    assert model.profile["max_input_tokens"] == 128_000, "other profile keys survive"


def test_declaring_image_support_on_a_profileless_model_is_safe() -> None:
    from novacode_cli.agents.core_agent import _declare_image_support

    _declare_image_support(object())  # no profile, not a chat model — a no-op


def test_webp_is_sent_to_a_multimodal_model_as_jpeg() -> None:
    """Ollama rejects WebP ("invalid image input"); PNG/JPEG are accepted everywhere."""
    from langchain_core.messages import ToolMessage
    from PIL import Image

    from novacode_cli.bootstrap.vision_router import _portable_images

    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (200, 10, 10, 128)).save(buffer, format="WEBP")
    data = base64.b64encode(buffer.getvalue()).decode()
    webp = {"type": "image", "base64": data, "mime_type": "image/webp"}
    png = {"type": "image", "base64": "iVBORw0KGgo=", "mime_type": "image/png"}
    messages = [ToolMessage(content=[webp, png], tool_call_id="t", name="read_file")]

    out = _portable_images(messages)

    block, kept = out[0].content
    assert block["mime_type"] == "image/jpeg"
    assert kept == png
    assert Image.open(io.BytesIO(base64.b64decode(block["base64"]))).format == "JPEG"
    assert out[0].tool_call_id == "t"
    assert messages[0].content[0]["mime_type"] == "image/webp", "copy-on-write"
    assert _portable_images(out) is out, "nothing left to convert: same list back"
