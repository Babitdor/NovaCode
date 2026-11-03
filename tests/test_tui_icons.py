"""Terminal icons follow the theme without changing conversation content."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from rich.cells import cell_len
from textual.color import Color
from textual.widgets import Static

from novacode_cli import ui_events as ev
from novacode_cli.tui.app import _render_bg_event
from novacode_cli.tui.icons import _TEXT_ICONS, text_icon
from novacode_cli.tui.screens import RemoteScreen
from novacode_cli.tui.widgets import PromptInput
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
from tests.test_tui_home_preferences import _app, no_system_sampling  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("original", "expected"),
    [("📄", "▤"), ("🛠️", "⌘"), ("⏳", "◷"), ("🧠", "◈"), ("👩‍💻", "◇"), ("◆", "◆")],
)
def test_event_icons_have_text_presentation(original: str, expected: str) -> None:
    assert text_icon(original) == expected


def test_builtin_icon_replacements_fit_one_terminal_cell() -> None:
    assert all(cell_len(symbol) == 1 for symbol in _TEXT_ICONS.values())
    assert all("\ufe0f" not in symbol for symbol in _TEXT_ICONS.values())


def test_background_tool_icon_changes_without_touching_arguments() -> None:
    event = ev.ToolCall(
        name="read_file", display_str="read_file('notes📁.md')", icon="📄", call_id="one"
    )
    pending: dict[str, str] = {}
    _render_bg_event(event, lambda _: None, lambda _: None, lambda _: "", pending)
    assert "▤ read_file('notes📁.md')" in pending["one"]
    assert event.icon == "📄"
    assert event.display_str == "read_file('notes📁.md')"


@pytest.mark.timeout(30)
async def test_remote_icons_recolor_and_keep_status_meanings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("novacode_cli.remote.config.load_remote_config", dict)
    app = _app()
    async with app.run_test(size=(116, 42)) as pilot:
        app.session_state._remote_bridge_manager = SimpleNamespace(
            active_bridges=[
                {"platform": "telegram", "status": "running", "chat_id": "test-chat"},
                {"platform": "discord", "status": "connecting...", "chat_id": "test-channel"},
                {"platform": "rootapp", "status": "error: unavailable", "chat_id": "test-channel"},
            ],
            _bridges={},
        )
        screen = RemoteScreen(app.session_state)
        app.push_screen(screen)
        await pilot.pause()
        for theme in ("tokyo-night", "textual-light"):
            app.theme = theme
            await pilot.pause()
            screen._refresh()
            body = screen.query_one("#remote-status", Static).render()
            palette = app._palette
            for label, color in (
                ("● Telegram", palette.success),
                ("◷ Discord", palette.warning),
                ("✕ Rootapp", palette.error),
            ):
                assert label in body.plain
                style = body.get_style_at_offset(body.plain.index(label))
                assert style.foreground is not None
                assert style.foreground.hex.lower() == color
            assert not any(character in body.plain for character in "🟢🟡🔴📡🔗")
            heading = screen.query_one("#modal-title", Static)
            assert "↗ Remote" in heading.render().plain
            assert heading.styles.color == Color.parse(app.get_css_variables()["accent"])
            (tmp_path / f"{theme}-remote.svg").write_text(app.export_screenshot(), encoding="utf-8")


@pytest.mark.timeout(30)
async def test_event_icon_and_user_draft_stay_separate() -> None:
    app = _app()
    async with app.run_test(size=(100, 34)):
        prompt = app.query_one("#prompt", PromptInput)
        prompt.text = "Keep my draft 📁🙂"
        event = ev.ContextMessage("Review notes 📁", event_type="nova_review_start", icon="🧠")
        await app._render(event)
        assert app._nova_status == "◈ Review notes 📁"
        assert event.icon == "🧠"
        assert event.message == "Review notes 📁"
        assert prompt.text == "Keep my draft 📁🙂"
        assert prompt.placeholder == ""
        assert not prompt.highlight_cursor_line
