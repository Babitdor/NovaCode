"""Tests for vision captioning — images become text before the main model sees them.

Covers:
- `caption_images`: OOB vision call with graceful degradation
- `_collect_image_urls` / `_latest_user_text` helpers
- `_extract_image_urls`, `_strip_image_content`, `_strip_all_images`
- VisionCaptionMiddleware: read_file tool-result captioning + model-call strip net
- Paste ingestion captioning in `prepare_input_content`
- Vision model config round-trip (NovaConfig)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

if TYPE_CHECKING:
    from collections.abc import Iterator

import novacode_cli.bootstrap.vision_router as vr
from novacode_cli.bootstrap.vision_router import (
    VisionCaptionMiddleware,
    _collect_image_urls,
    _extract_image_urls,
    _latest_user_text,
    _strip_all_images,
    _strip_image_content,
    caption_images,
)
from novacode_cli.config.nova_config import NovaConfig


@pytest.fixture(autouse=True)
def _reset_vision_globals() -> Iterator[None]:
    """Isolate the process-wide vision-model cache between tests."""
    vr._vision_model_instance = None
    vr._vision_model_errored = False
    vr._vision_cooldown_remaining = 0
    yield
    vr._vision_model_instance = None
    vr._vision_model_errored = False
    vr._vision_cooldown_remaining = 0


# =========================================================================
# caption_images — the OOB vision call (graceful degradation)
# =========================================================================


class TestCaptionImages:
    async def test_empty_urls_returns_empty(self):
        assert await caption_images([], "anything") == ""

    async def test_returns_caption_text(self, monkeypatch: pytest.MonkeyPatch):
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=SimpleNamespace(content="a red error dialog"))
        monkeypatch.setattr(vr, "get_vision_model", lambda: model)
        out = await caption_images(["data:image/png;base64,abc"], task_hint="fix the bug")
        assert out == "a red error dialog"

    async def test_unavailable_returns_placeholder(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(vr, "get_vision_model", lambda: None)
        out = await caption_images(["data:image/png;base64,abc"], "")
        assert "unavailable" in out
        assert "base64" not in out  # never leak the image

    async def test_model_error_degrades_to_placeholder(self, monkeypatch: pytest.MonkeyPatch):
        model = MagicMock()
        model.ainvoke = AsyncMock(side_effect=RuntimeError("does not support image input"))
        monkeypatch.setattr(vr, "get_vision_model", lambda: model)
        out = await caption_images(["data:image/png;base64,abc"], "")
        assert "failed" in out
        # A hard failure starts a cooldown (not a permanent session disable).
        assert vr._vision_cooldown_remaining == vr._VISION_FAILURE_COOLDOWN
        assert vr._vision_model_errored is False


# =========================================================================
# _collect_image_urls / _latest_user_text
# =========================================================================


class TestCollectImageUrls:
    def test_from_content_blocks(self):
        msg = ToolMessage(
            content="",
            name="read_file",
            tool_call_id="t",
            content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
        )
        assert _collect_image_urls(msg) == ["data:image/png;base64,abc"]

    def test_from_content_image_url(self):
        msg = HumanMessage(
            content=[
                {"type": "text", "text": "x"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
            ]
        )
        assert _collect_image_urls(msg) == ["data:image/png;base64,xyz"]

    def test_dedup(self):
        msg = HumanMessage(
            content=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,dup"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,dup"}},
            ]
        )
        assert _collect_image_urls(msg) == ["data:image/png;base64,dup"]

    def test_text_only(self):
        assert _collect_image_urls(HumanMessage(content="hello")) == []


class TestLatestUserText:
    def test_string_content(self):
        msgs = [
            HumanMessage(content="first"),
            AIMessage(content="reply"),
            HumanMessage(content="latest"),
        ]
        assert _latest_user_text(msgs) == "latest"

    def test_list_content(self):
        msgs = [HumanMessage(content=[{"type": "text", "text": "look at this"}])]
        assert _latest_user_text(msgs) == "look at this"

    def test_no_human(self):
        assert _latest_user_text([AIMessage(content="hi")]) == ""


# =========================================================================
# _extract_image_urls
# =========================================================================


class TestExtractImageUrls:
    def test_toolmessage_with_content_blocks(self):
        msg = ToolMessage(
            content="",
            name="read_file",
            tool_call_id="tc1",
            content_blocks=[{"type": "image", "base64": "abc123", "mime_type": "image/png"}],
        )
        assert _extract_image_urls(msg) == ["data:image/png;base64,abc123"]

    def test_toolmessage_infers_mime_from_path(self):
        msg = ToolMessage(
            content="",
            name="read_file",
            tool_call_id="tc1",
            content_blocks=[{"type": "image", "base64": "def"}],
            additional_kwargs={"read_file_path": "/photo.jpg"},
        )
        assert _extract_image_urls(msg)[0].startswith("data:image/jpeg;base64,def")

    def test_toolmessage_no_mime_fallsback_to_png(self):
        msg = ToolMessage(
            content="",
            name="read_file",
            tool_call_id="tc1",
            content_blocks=[{"type": "image", "base64": "ghi"}],
        )
        assert _extract_image_urls(msg)[0].startswith("data:image/png;base64,ghi")

    def test_text_only_message(self):
        assert _extract_image_urls(HumanMessage(content="hello")) == []


# =========================================================================
# _strip_image_content / _strip_all_images
# =========================================================================


class TestStripImageContent:
    def test_plain_string_message_noop(self):
        msg = HumanMessage(content="hello world")
        assert _strip_image_content(msg) is msg

    def test_toolmessage_no_content_blocks_noop(self):
        msg = ToolMessage(content="result text", name="grep", tool_call_id="tc1")
        assert _strip_image_content(msg) is msg

    def test_toolmessage_with_content_blocks_image_stripped(self):
        msg = ToolMessage(
            content="file loaded",
            name="read_file",
            tool_call_id="tc1",
            content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
        )
        result = _strip_image_content(msg)
        assert result is not msg
        assert isinstance(result, ToolMessage)
        remaining = getattr(result, "content_blocks", None) or []
        assert all(not (isinstance(b, dict) and b.get("type") == "image") for b in remaining)

    def test_human_message_with_image_url_in_content(self):
        msg = HumanMessage(
            content=[
                {"type": "text", "text": "describe this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ]
        )
        result = _strip_image_content(msg)
        assert result is not msg
        assert isinstance(result.content, list)
        assert all(b.get("type") != "image_url" for b in result.content)
        assert any("describe this image" in b.get("text", "") for b in result.content)


class TestStripAllImages:
    def test_no_images_returns_original_list(self):
        msgs = [HumanMessage(content="a"), AIMessage(content="b")]
        assert _strip_all_images(msgs) is msgs

    def test_mixed_list_copy_on_write(self):
        clean = HumanMessage(content="hello")
        dirty = HumanMessage(
            content=[
                {"type": "text", "text": "desc"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ]
        )
        result = _strip_all_images([clean, dirty])
        assert result[0] is clean
        assert result[1] is not dirty
        assert _collect_image_urls(result[1]) == []


# =========================================================================
# VisionCaptionMiddleware — tool-result captioning
# =========================================================================


def _tool_request(name: str, args: dict, state_messages: list | None = None) -> MagicMock:
    req = MagicMock()
    req.tool_call = {"name": name, "args": args}
    req.state = {"messages": state_messages or []}
    return req


class TestVisionCaptionToolHook:
    async def test_read_file_image_captioned_to_text(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            vr, "caption_images", AsyncMock(return_value="screenshot of a login form")
        )
        mw = VisionCaptionMiddleware()
        image_tm = ToolMessage(
            content="",
            name="read_file",
            tool_call_id="tc1",
            content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
        )
        handler = AsyncMock(return_value=image_tm)
        req = _tool_request(
            "read_file", {"file_path": "/login.png"}, [HumanMessage(content="check the login UI")]
        )
        out = await mw.awrap_tool_call(req, handler)
        assert isinstance(out, ToolMessage)
        assert "screenshot of a login form" in out.content
        assert "/login.png" in out.content
        assert _collect_image_urls(out) == []  # no image leaks downstream

    async def test_non_read_file_passthrough(self):
        mw = VisionCaptionMiddleware()
        tm = ToolMessage(content="grep results", name="grep", tool_call_id="t")
        handler = AsyncMock(return_value=tm)
        out = await mw.awrap_tool_call(_tool_request("grep", {}), handler)
        assert out is tm

    async def test_read_file_no_image_passthrough(self):
        mw = VisionCaptionMiddleware()
        tm = ToolMessage(content="print('hi')", name="read_file", tool_call_id="t")
        handler = AsyncMock(return_value=tm)
        out = await mw.awrap_tool_call(_tool_request("read_file", {"file_path": "/a.py"}), handler)
        assert out is tm

    async def test_vision_unavailable_returns_text_not_image(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            vr, "caption_images", AsyncMock(return_value="[image: vision model unavailable]")
        )
        mw = VisionCaptionMiddleware()
        image_tm = ToolMessage(
            content="",
            name="read_file",
            tool_call_id="tc1",
            content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
        )
        handler = AsyncMock(return_value=image_tm)
        out = await mw.awrap_tool_call(_tool_request("read_file", {"file_path": "/a.png"}), handler)
        assert isinstance(out, ToolMessage)
        assert _collect_image_urls(out) == []
        assert "unavailable" in out.content


class TestVisionCaptionModelHook:
    async def test_strips_residual_images(self):
        mw = VisionCaptionMiddleware()
        req = MagicMock()
        req.messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "x"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]
            )
        ]
        req.override.return_value = req
        handler = AsyncMock(return_value="ok")
        out = await mw.awrap_model_call(req, handler)
        assert out == "ok"
        req.override.assert_called_once()
        forwarded = req.override.call_args.kwargs["messages"]
        assert _collect_image_urls(forwarded[0]) == []


# =========================================================================
# Capability-aware pass-through — multimodal main model
# =========================================================================


class TestMultimodalPassThrough:
    """When the main model is multimodal, images flow straight through."""

    async def test_tool_call_returns_image_unchanged(self, monkeypatch: pytest.MonkeyPatch):
        # caption_images must NOT be called — the image goes to the main model.
        caption = AsyncMock(return_value="should not be used")
        monkeypatch.setattr(vr, "caption_images", caption)
        mw = VisionCaptionMiddleware(main_model_supports_images=True)
        image_tm = ToolMessage(
            content="",
            name="read_file",
            tool_call_id="tc1",
            content_blocks=[{"type": "image", "base64": "abc", "mime_type": "image/png"}],
        )
        handler = AsyncMock(return_value=image_tm)
        out = await mw.awrap_tool_call(
            _tool_request("read_file", {"file_path": "/a.png"}), handler
        )
        assert out is image_tm  # unchanged — image preserved
        assert _collect_image_urls(out) == ["data:image/png;base64,abc"]
        caption.assert_not_called()

    async def test_model_call_does_not_strip(self):
        mw = VisionCaptionMiddleware(main_model_supports_images=True)
        req = MagicMock()
        req.messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "x"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]
            )
        ]
        handler = AsyncMock(return_value="ok")
        out = await mw.awrap_model_call(req, handler)
        assert out == "ok"
        req.override.assert_not_called()  # no stripping
        assert _collect_image_urls(req.messages[0]) == ["data:image/png;base64,abc"]

    async def test_default_is_text_only_behaviour(self):
        # Default (False) must preserve caption-and-strip.
        mw = VisionCaptionMiddleware()
        assert mw.main_model_supports_images is False


# =========================================================================
# Vision failure cooldown — one failure must not kill the session
# =========================================================================


class TestVisionCooldown:
    async def test_failure_starts_cooldown_not_permanent(self, monkeypatch: pytest.MonkeyPatch):
        model = MagicMock()
        model.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(vr, "get_vision_model", lambda: model)
        out = await caption_images(["data:image/png;base64,abc"], "")
        assert "failed" in out
        # Cooldown armed, but NOT a permanent disable.
        assert vr._vision_cooldown_remaining == vr._VISION_FAILURE_COOLDOWN
        assert vr._vision_model_errored is False

    async def test_cooldown_skips_then_retries(self, monkeypatch: pytest.MonkeyPatch):
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=SimpleNamespace(content="a caption"))
        monkeypatch.setattr(vr, "get_vision_model", lambda: model)
        vr._vision_cooldown_remaining = 2
        # First two calls are skipped (cooldown), third retries and succeeds.
        assert "failed" in await caption_images(["data:image/png;base64,abc"], "")
        assert "failed" in await caption_images(["data:image/png;base64,abc"], "")
        assert await caption_images(["data:image/png;base64,abc"], "") == "a caption"
        model.ainvoke.assert_awaited_once()


# =========================================================================
# Paste ingestion captioning — prepare_input_content
# =========================================================================


class TestPasteIngestionCaptioning:
    async def test_pasted_image_becomes_text(self, monkeypatch: pytest.MonkeyPatch):
        import novacode_cli.core.input_preparation as ip

        # Captioning only applies to a TEXT-ONLY main model; a multimodal one is
        # handed the image blocks instead (test_vision_multimodal_routing.py).
        monkeypatch.setattr(ip, "_main_model_can_see_images", lambda: False)
        monkeypatch.setattr(vr, "caption_images", AsyncMock(return_value="a bar chart of sales"))

        class _FakeImg:
            def to_data_url(self) -> str:
                return "data:image/png;base64,abc"

            def to_message_content(self) -> dict:
                return {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}

        tracker = MagicMock()
        tracker.get_images.return_value = [_FakeImg()]

        out = await ip.prepare_input_content(
            "describe this chart", image_tracker=tracker, skip_file_mentions=True
        )
        assert isinstance(out, str)
        assert "describe this chart" in out
        assert "a bar chart of sales" in out
        assert "data:image" not in out  # image never enters the conversation

    async def test_multimodal_main_model_gets_image_blocks_not_a_caption(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The counterpart: a capable model reads the image itself."""
        import novacode_cli.core.input_preparation as ip

        monkeypatch.setattr(ip, "_main_model_can_see_images", lambda: True)
        caption = AsyncMock(return_value="should not be used")
        monkeypatch.setattr(vr, "caption_images", caption)

        class _FakeImg:
            def to_data_url(self) -> str:
                return "data:image/png;base64,abc"

            def to_message_content(self) -> dict:
                return {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}

        tracker = MagicMock()
        tracker.get_images.return_value = [_FakeImg()]

        out = await ip.prepare_input_content(
            "describe this chart", image_tracker=tracker, skip_file_mentions=True
        )
        assert isinstance(out, list)
        assert out[0]["type"] == "text"
        assert out[1]["type"] == "image_url"
        caption.assert_not_awaited()


# =========================================================================
# NovaConfig vision model config
# =========================================================================


class TestVisionModelConfig:
    def test_default_vision_config(self):
        cfg = NovaConfig().get_vision_model_config()
        assert cfg["provider"] == "ollama"
        assert cfg["model"] == "gemma4:31b-cloud"

    def test_set_and_get_vision_config(self):
        nc = NovaConfig()
        nc.set_vision_model_config("google", "gemini-2.0-flash-exp")
        cfg = nc.get_vision_model_config()
        assert cfg["provider"] == "google"
        assert cfg["model"] == "gemini-2.0-flash-exp"
        nc.clear_vision_model_config()

    def test_clear_vision_config_reverts_to_default(self):
        nc = NovaConfig()
        nc.set_vision_model_config("openai", "gpt-4o")
        nc.clear_vision_model_config()
        cfg = nc.get_vision_model_config()
        assert cfg["provider"] == "ollama"


class TestMainModelMultimodalOverride:
    def test_default_is_none(self):
        nc = NovaConfig()
        nc.set_main_model_multimodal(None)
        assert nc.get_main_model_multimodal() is None

    def test_set_true_and_false(self):
        nc = NovaConfig()
        nc.set_main_model_multimodal(True)
        assert nc.get_main_model_multimodal() is True
        nc.set_main_model_multimodal(False)
        assert nc.get_main_model_multimodal() is False
        nc.set_main_model_multimodal(None)
        assert nc.get_main_model_multimodal() is None


# ── capability detection: the model's own profile beats our pattern list ─────


class _Profiled:
    """A bound chat model carrying a LangChain ModelProfile."""

    def __init__(self, image_inputs: bool | None) -> None:
        self.profile = {"max_input_tokens": 128_000}
        if image_inputs is not None:
            self.profile["image_inputs"] = image_inputs


def _fresh_caps(monkeypatch, *, override=None, model="some/unlisted-vl-model"):
    from novacode_cli.config import model_capabilities as mc

    monkeypatch.setattr(mc, "_bound_profile_support", None, raising=False)

    class _Cfg:
        def get_main_model_multimodal(self):  # noqa: ANN001, ANN201
            return override

        def get_model_config(self):  # noqa: ANN201
            return {"provider": "openai", "model": model}

    monkeypatch.setattr("novacode_cli.config.nova_config.NovaConfig", lambda: _Cfg())
    return mc


def test_an_unlisted_model_is_believed_when_its_profile_says_it_sees_images(monkeypatch):
    """The pattern list cannot know every model; the provider's profile does."""
    mc = _fresh_caps(monkeypatch)
    assert mc.resolve_main_model_multimodal(None) is False, "unlisted, nothing bound yet"

    mc.note_bound_model(_Profiled(image_inputs=True))
    assert mc.resolve_main_model_multimodal(None) is True


def test_a_profile_saying_no_is_believed_too(monkeypatch):
    mc = _fresh_caps(monkeypatch, model="gpt-4o")
    mc.note_bound_model(_Profiled(image_inputs=False))
    assert mc.resolve_main_model_multimodal(None) is False


def test_a_model_without_a_profile_falls_back_to_the_pattern_list(monkeypatch):
    mc = _fresh_caps(monkeypatch, model="gpt-4o")
    mc.note_bound_model(_Profiled(image_inputs=None))
    assert mc.resolve_main_model_multimodal(None) is True


def test_the_user_override_still_wins(monkeypatch):
    mc = _fresh_caps(monkeypatch, override=True)
    mc.note_bound_model(_Profiled(image_inputs=False))
    assert mc.resolve_main_model_multimodal(None) is True


def test_a_failed_caption_forbids_pixel_archaeology() -> None:
    """The symptom: told an image "was not attached", the model writes a decoder."""
    from novacode_cli.bootstrap import vision_router as vr

    for placeholder in (vr._VISION_UNAVAILABLE, vr._VISION_FAILED, vr._VISION_EMPTY):
        assert "Do not try to inspect the image yourself" in placeholder
        assert vr.is_vision_failure(placeholder), "still recognised as a failure"
