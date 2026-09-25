"""Ctrl+V in the prompt pastes a clipboard image as an attachment.

The read is platform-specific and blocking (PIL on Windows, a subprocess on
Linux/macOS), so it runs on a worker and the widget falls back to a normal text
paste when there is no image — Ctrl+V must never become a broken key.
"""

from __future__ import annotations

import asyncio
import base64
import io

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")


def _png_b64() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _image():
    from novacode_cli.image_utils import ImageData

    return ImageData(base64_data=_png_b64(), format="png", placeholder="[image]")


def _host(tracker):
    from textual.app import App, ComposeResult

    from novacode_cli.tui.widgets import PromptInput

    class Host(App):
        image_tracker = tracker

        def compose(self) -> ComposeResult:
            yield PromptInput(id="p", on_clipboard_image=self._on_clipboard_image)

        def _on_clipboard_image(self, image):
            return tracker.add_image(image)

    return Host


async def _press_ctrl_v(prompt, reader):
    """Send ctrl+v with the clipboard reader stubbed to *reader*."""
    from textual import events

    prompt._read_clipboard_image = staticmethod(reader)
    prompt.post_message(events.Key("ctrl+v", None))


# ── the happy path ───────────────────────────────────────────────────────────


def test_ctrl_v_inserts_an_image_placeholder_and_tracks_it():
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    app = _host(tracker)()

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            await _press_ctrl_v(prompt, _image)
            for _ in range(6):
                await pilot.pause()
            return prompt.text, tracker.list_images()

    text, images = asyncio.run(drive())
    assert text == "[image-1] ", f"unexpected input text: {text!r}"
    assert [i["id"] for i in images] == ["image-1"]


def test_pasted_image_is_attached_to_the_conversation():
    """The image must survive into the prompt-prep content, not just the box."""
    import asyncio as _asyncio

    from novacode_cli.core.input_preparation import prepare_input_content
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    tracker.add_image(_image())

    # The image must reach the model either way: as image blocks for a multimodal
    # main model, or as a caption for a text-only one. This machine's configured
    # model is multimodal, so pin text-only to exercise the captioning branch.
    import novacode_cli.core.input_preparation as ip

    orig_can_see = ip._main_model_can_see_images
    ip._main_model_can_see_images = lambda: False

    async def fake_captions(_images, *a, **k):  # noqa: ANN002, ANN003, ANN002
        return "a small red square"

    import novacode_cli.bootstrap.vision_router as vr

    orig = vr.caption_images
    vr.caption_images = fake_captions
    try:
        out = _asyncio.run(
            prepare_input_content("what is this?", image_tracker=tracker)
        )
    finally:
        vr.caption_images = orig
        ip._main_model_can_see_images = orig_can_see
    assert "a small red square" in out
    assert "what is this?" in out


# ── the fallback: Ctrl+V must not break text paste ───────────────────────────


def test_ctrl_v_without_an_image_does_not_insert_a_placeholder():
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    app = _host(tracker)()

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            await _press_ctrl_v(prompt, lambda: None)
            for _ in range(6):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == ""
    assert tracker.list_images() == []


def test_declining_the_image_falls_back_to_text_paste():
    """An app that returns "" must not get a dangling placeholder."""
    from textual.app import App, ComposeResult

    from novacode_cli.tui.widgets import PromptInput

    class Host(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="p", on_clipboard_image=lambda _img: "")

    app = Host()

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            await _press_ctrl_v(prompt, _image)
            for _ in range(6):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == ""


def test_no_image_tracker_configured_is_a_plain_paste():
    """With no callback at all, ctrl+v must still be handled (no crash)."""
    from textual.app import App, ComposeResult

    from novacode_cli.tui.widgets import PromptInput

    class Host(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="p")

    app = Host()

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            await _press_ctrl_v(prompt, _image)
            for _ in range(4):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == ""


def test_reader_failure_is_swallowed():
    """A raising clipboard reader must not take the input box down."""
    from novacode_cli.input_utils import ImageTracker

    tracker = ImageTracker()
    app = _host(tracker)()

    def boom():
        raise RuntimeError("clipboard exploded")

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            await _press_ctrl_v(prompt, boom)
            for _ in range(4):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == ""


# ── the path that actually fires in Windows Terminal ─────────────────────────
#
# Windows Terminal owns ctrl+v: the key never reaches the widget at all. What
# it delivers instead is a bracketed paste — and with an image on the clipboard
# that paste is EMPTY. So the empty-paste handler is the path that really runs
# for the user, and the ctrl+v branch above is the fallback for terminals that
# pass the key through.


async def _send_empty_paste(prompt, reader):
    from textual import events

    prompt._read_clipboard_image = staticmethod(reader)
    prompt.post_message(events.Paste(""))


def test_empty_paste_picks_up_a_clipboard_image():
    """The real Windows Terminal path: empty paste -> image."""
    from textual.app import App, ComposeResult

    from novacode_cli.input_utils import ImageTracker
    from novacode_cli.tui.widgets import PromptInput

    tracker = ImageTracker()

    class Host(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="p", on_clipboard_image=lambda i: tracker.add_image(i))

    app = Host()

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            await _send_empty_paste(prompt, _image)
            for _ in range(8):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == "[image-1] "
    assert [i["id"] for i in tracker.list_images()] == ["image-1"]


def test_empty_paste_without_an_image_leaves_input_alone():
    """An empty paste with no image (or no callback) must stay a no-op."""
    from textual.app import App, ComposeResult

    from novacode_cli.tui.widgets import PromptInput

    class Host(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="p", on_clipboard_image=lambda i: "image-1")

    app = Host()

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            await _send_empty_paste(prompt, lambda: None)
            for _ in range(6):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == ""


def test_empty_paste_is_a_noop_with_no_image_callback():
    """A genuinely empty Ctrl+V must not start a worker or insert anything."""
    from textual.app import App, ComposeResult

    from novacode_cli.tui.widgets import PromptInput

    class Host(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="p")

    app = Host()

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            prompt.post_message(__import__("textual.events", fromlist=["Paste"]).Paste(""))
            for _ in range(4):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == ""


def test_a_real_text_paste_still_inserts_text():
    """Sensitivity: the image path must not hijack ordinary text pastes."""
    from textual.app import App, ComposeResult

    from novacode_cli.input_utils import ImageTracker
    from novacode_cli.tui.widgets import PromptInput

    tracker = ImageTracker()

    class Host(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="p", on_clipboard_image=lambda i: tracker.add_image(i))

    app = Host()

    async def drive():
        from textual import events

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#p")
            prompt.focus()
            await pilot.pause()
            # An image IS on the clipboard, but the paste carries text.
            prompt._read_clipboard_image = staticmethod(_image)
            prompt.post_message(events.Paste("pasted text"))
            for _ in range(6):
                await pilot.pause()
            return prompt.text

    assert asyncio.run(drive()) == "pasted text"
    assert tracker.list_images() == []
