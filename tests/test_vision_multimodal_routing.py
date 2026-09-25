"""A pasted image goes to whichever model can actually read it.

Two defects, both user-visible as "[image: vision captioning failed]" while
running a model that can see images perfectly well:

1. ``prepare_input_content`` force-captioned through the auxiliary vision model
   regardless of whether the main model was multimodal, so a capable model was
   bypassed and a missing/broken vision model broke the paste outright.
2. A *failure* placeholder is a truthy string, so ``if captions`` accepted it and
   returned the notice as though it were a description — making the existing
   "fall back to image blocks" path unreachable.

The content shape is the contract: a ``list`` means "image blocks, the model
reads it"; a ``str`` means "already captioned to text".
"""

from __future__ import annotations

import asyncio
import base64
import io

import pytest


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


def test_text_only_model_still_gets_a_caption(monkeypatch):
    out = _prepare(monkeypatch, multimodal=False, caption_fn=_real_caption)
    assert isinstance(out, str), "a text-only model cannot receive image blocks"
    assert "[Attached image: a PDF viewer showing page 9]" in out
    assert "what is this?" in out


# ── a FAILED caption must fall through, not become the answer ───────────────


@pytest.mark.parametrize(
    "placeholder_name",
    ["_VISION_FAILED", "_VISION_UNAVAILABLE", "_VISION_EMPTY"],
)
def test_a_failure_placeholder_falls_through_to_image_blocks(monkeypatch, placeholder_name):
    """A truthy failure string must not be mistaken for a caption."""
    from novacode_cli.bootstrap import vision_router

    placeholder = getattr(vision_router, placeholder_name)

    async def returns_placeholder(image_urls, *args, **kwargs):
        return placeholder

    out = _prepare(monkeypatch, multimodal=False, caption_fn=returns_placeholder)
    assert isinstance(out, list), (
        f"{placeholder_name} is a truthy string and was returned as if it were a "
        "caption; the image blocks should have been passed through instead"
    )
    assert out[1]["type"] == "image_url"


def test_a_raising_caption_falls_through_to_image_blocks(monkeypatch):
    async def boom(image_urls, *args, **kwargs):
        raise RuntimeError("vision exploded")

    out = _prepare(monkeypatch, multimodal=False, caption_fn=boom)
    assert isinstance(out, list)
    assert out[1]["type"] == "image_url"


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
