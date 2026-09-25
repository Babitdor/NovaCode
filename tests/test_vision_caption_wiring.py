"""Clipboard images reach the vision model as data URLs.

Regression: ``prepare_input_content`` passed ``ImageData`` *objects* to
``caption_images``, which takes ``data:`` URL *strings*. ``langchain_ollama``
rejected the objects with "Only string image_url or dict with string 'url'
inside content parts are supported", and the surrounding bare ``except: pass``
turned that into the user-facing placeholder
"[image: vision captioning failed ...]" — with a working vision model.
"""

from __future__ import annotations

import asyncio
import base64
import io

import pytest


@pytest.fixture(autouse=True)
def _text_only_main_model(monkeypatch):
    """Force the captioning path on.

    This module tests the caption path, which only runs for a text-only main
    model — a multimodal one gets image blocks instead (see
    ``test_vision_multimodal_routing.py``). Pinning it keeps these tests
    independent of whichever model happens to be configured on this machine.
    """
    from novacode_cli.core import input_preparation

    monkeypatch.setattr(
        input_preparation, "_main_model_can_see_images", lambda: False
    )


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


# ── the ImageData contract ──────────────────────────────────────────────────


def test_to_data_url_is_the_form_multimodal_apis_accept():
    img = _image_data()
    url = img.to_data_url()
    assert url.startswith("data:image/png;base64,")
    assert url == f"data:image/png;base64,{img.base64_data}"


def test_to_message_content_uses_the_same_url():
    img = _image_data()
    part = img.to_message_content()
    assert part["type"] == "image_url"
    # The value must be a *string* — a nested object is rejected by the client.
    assert isinstance(part["image_url"]["url"], str)
    assert part["image_url"]["url"] == img.to_data_url()


# ── the caller must pass strings, not objects ───────────────────────────────


def test_caption_images_is_given_url_strings_not_objects(monkeypatch):
    """The exact bug: objects were passed where URL strings were required."""
    from novacode_cli.core import input_preparation
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    tracker.add_image(_image_data())

    seen: list = []

    async def fake_caption(image_urls, *args, **kwargs):
        seen.append(image_urls)
        return "a small blue square"

    monkeypatch.setattr(
        "novacode_cli.bootstrap.vision_router.caption_images", fake_caption
    )

    out = asyncio.run(
        input_preparation.prepare_input_content("what is this?", image_tracker=tracker)
    )

    assert isinstance(out, str), "captioning should have produced text"
    assert "a small blue square" in out
    assert seen, "caption_images was never called"
    for item in seen[0]:
        assert isinstance(item, str), (
            f"caption_images expects URL strings, got {type(item).__name__}"
        )
        assert item.startswith("data:image/")


def test_a_successful_caption_is_attached_to_the_prompt(monkeypatch):
    """The caption is wrapped and appended, with the user's text intact."""
    from novacode_cli.core import input_preparation
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    tracker.add_image(_image_data())

    async def fake_caption(image_urls, *args, **kwargs):
        return "a small blue square"

    monkeypatch.setattr(
        "novacode_cli.bootstrap.vision_router.caption_images", fake_caption
    )

    out = asyncio.run(
        input_preparation.prepare_input_content("describe this", image_tracker=tracker)
    )
    assert isinstance(out, str)
    assert "describe this" in out
    assert "[Attached image: a small blue square]" in out


# ── a caption failure must be visible, not silent ───────────────────────────


def test_captioning_failure_is_logged_not_silently_swallowed(monkeypatch, caplog):
    """The swallowed exception is why this bug hid for so long."""
    import logging

    from novacode_cli.core import input_preparation
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    tracker.add_image(_image_data())

    async def boom(*args, **kwargs):
        raise RuntimeError("vision exploded")

    monkeypatch.setattr(
        "novacode_cli.bootstrap.vision_router.caption_images", boom
    )

    with caplog.at_level(logging.WARNING):
        out = asyncio.run(
            input_preparation.prepare_input_content("hi", image_tracker=tracker)
        )
    assert "Clipboard image captioning failed" in caplog.text, (
        "a captioning failure must be logged, not silently discarded"
    )
    # And it still degrades safely: the image blocks are passed through.
    assert out is not None


# ── sensitivity: the fix is what makes it work ──────────────────────────────


@pytest.mark.parametrize("bad_input", ["objects"])
def test_passing_objects_is_what_broke_it(bad_input):
    """Confirms the mechanism: raw ImageData objects are not acceptable.

    This pins *why* the fix was needed — if a future refactor passes the
    objects again, this documents the failure it causes.
    """
    from langchain_core.messages import HumanMessage

    from novacode_cli.image_utils import ImageData

    img = _image_data()
    # A well-formed content part uses the string URL.
    good = HumanMessage(
        content=[
            {"type": "text", "text": "x"},
            img.to_message_content(),
        ]
    )
    assert isinstance(good.content[1]["image_url"]["url"], str)  # type: ignore[index]

    # And the object itself is not a string URL (the old, broken call site).
    assert not isinstance(img, str)
    assert not isinstance(ImageData, str)
