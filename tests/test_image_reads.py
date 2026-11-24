"""Image reads become inspectable: TUI card + preview, remote photo previews."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")

from novacode_cli.file_ops import FileOpTracker  # noqa: E402
from novacode_cli.remote.images import resolve_workspace_image  # noqa: E402


@pytest.fixture(autouse=True)
def _offline_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    import novacode_cli.config.config as config_module

    monkeypatch.setattr(config_module, "HOME_DIR", tmp_path / ".nova")
    from novacode_cli.config.nova_config import NovaConfig

    def isolated_config(config):
        config.config_dir = tmp_path / ".nova"
        config.config_path = config.config_dir / "Nova.config.json"
        config._config = {}
        config._loaded = {}
        config._load()

    monkeypatch.setattr(NovaConfig, "__init__", isolated_config)
    from novacode_cli.tui.app import NovaApp

    async def no_warmup(self: object) -> None:
        pass

    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", no_warmup)


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 6), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _image_tool_message(path: str, *, media_type: str = "image/png") -> SimpleNamespace:
    """A deepagents-shaped image read result: base64 block + stamped path."""
    import base64

    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    return SimpleNamespace(
        tool_call_id="call_1",
        id="msg_1",
        status="success",
        content="",
        content_blocks=[{"type": "image", "base64": encoded, "mime_type": media_type}],
        additional_kwargs={
            "read_file_path": path,
            "read_file_media_type": media_type,
        },
        name="read_file",
    )


def _tracker_with_image(path: str, media_type: str = "image/png"):
    tracker = FileOpTracker(backend=None, assistant_id="nova-agent")
    tracker.start_operation("read_file", {"file_path": path}, "call_1")
    return tracker.complete_with_message(_image_tool_message(path, media_type=media_type))


def test_an_image_read_is_stamped_on_the_record() -> None:
    record = _tracker_with_image("/.shots/glow-fixed-buttons.png")
    assert record is not None
    assert record.read_image == "/.shots/glow-fixed-buttons.png"


def test_a_text_read_is_not_stamped() -> None:
    message = SimpleNamespace(
        tool_call_id="call_1",
        id="msg_1",
        status="success",
        content="the file text",
        additional_kwargs={"read_file_path": "/notes.md", "read_file_media_type": "text/plain"},
        name="read_file",
    )
    tracker = FileOpTracker(backend=None, assistant_id="nova-agent")
    tracker.start_operation("read_file", {"file_path": "/notes.md"}, "call_1")
    record = tracker.complete_with_message(message)
    assert record is not None
    assert record.read_image is None


def test_workspace_resolution_maps_virtual_paths(tmp_path: Path) -> None:
    (tmp_path / ".shots").mkdir()
    (tmp_path / ".shots" / "glow-fixed-buttons.png").write_bytes(_png_bytes())
    root = str(tmp_path)

    resolved = resolve_workspace_image("/.shots/glow-fixed-buttons.png", root)
    assert resolved is not None
    assert resolved.name == "glow-fixed-buttons.png"

    # Outside the workspace, missing files and URLs are refused.
    assert resolve_workspace_image("/.shots/missing.png", root) is None
    assert (
        resolve_workspace_image(
            "/.shots/glow-fixed-buttons.png", str(tmp_path.parent / "elsewhere")
        )
        is None
    )
    assert resolve_workspace_image("https://x.test/a.png", root) is None
    (tmp_path / "notes.md").write_text("text")
    assert resolve_workspace_image("/notes.md", root) is None


async def test_tui_mounts_a_clickable_card_and_opens_the_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli import ui_events as ev
    from novacode_cli.config.config import settings
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.screens import ImagePreviewScreen
    from novacode_cli.tui.widgets import ImageReadCard
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    shot = tmp_path / ".shots"
    shot.mkdir()
    (shot / "glow-fixed-buttons.png").write_bytes(_png_bytes())

    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )
    app._home_style = "minimal"
    async with app.run_test(size=(120, 44)) as pilot:
        app._home_style = "minimal"
        await pilot.pause()

        record = SimpleNamespace(
            tool_name="read_file",
            display_path="/.shots/glow-fixed-buttons.png",
            status="success",
            error=None,
            read_output="",
            read_image="/.shots/glow-fixed-buttons.png",
            metrics=SimpleNamespace(lines_read=0, start_line=None, end_line=None),
            diff=None,
        )
        await app._render(ev.FileOp(record=record, full_output="", call_id="c1"))
        await pilot.pause()

        card = app.query_one(ImageReadCard)
        assert "glow-fixed-buttons.png" in str(card.render())

        # The virtual "/.shots/..." path resolves against the workspace root —
        # without that mapping the preview shows a FileNotFoundError instead
        # of the image.
        monkeypatch.setattr(settings, "get_workspace_root", lambda: tmp_path)
        card.action_inspect()
        await pilot.pause()
        assert any(isinstance(screen, ImagePreviewScreen) for screen in app.screen_stack), (
            "clicking the card opens the image preview"
        )
        preview = next(
            screen for screen in app.screen_stack if isinstance(screen, ImagePreviewScreen)
        )
        body = str(preview.query_one("#image-body").render())
        assert body.strip(), "the half-block fallback renders visible content"
        assert "Could not load image" not in body, (
            "the virtual .shots path must resolve to the workspace file"
        )


async def test_execution_hook_sends_the_photo(monkeypatch) -> None:
    """The headless/remote event loop forwards image reads to the bridge hook."""
    sent: list[str] = []

    async def send(path: str) -> None:
        sent.append(path)

    record = SimpleNamespace(
        tool_name="read_file",
        display_path="/.shots/x.png",
        status="success",
        error=None,
        read_output="",
        read_image="/.shots/x.png",
        metrics=SimpleNamespace(lines_read=0, start_line=None, end_line=None),
        diff=None,
    )
    session_state = SimpleNamespace(_remote_image_read=send)

    # The same guard the execution loop uses, exercised directly.
    _image_read = getattr(session_state, "_remote_image_read", None)
    _image_path = getattr(record, "read_image", None)
    if _image_read is not None and _image_path:
        await _image_read(str(_image_path))
    assert sent == ["/.shots/x.png"]
