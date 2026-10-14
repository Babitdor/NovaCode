"""Embedded terminal input and redraw behavior."""

from unittest.mock import Mock

import pytest
from textual.app import App, ComposeResult
from textual.events import MouseScrollUp

from novacode_cli.tui.terminal_view import (
    InlineTerminal,
    TerminalCommandScreen,
    terminal_key,
)


def test_terminal_keys_cover_interactive_menus():
    assert terminal_key("down") == "\x1b[B"
    assert terminal_key("up") == "\x1b[A"
    assert terminal_key("space") == " "
    assert terminal_key("enter") == "\r"
    assert terminal_key("ctrl+c") == "\x03"


def test_terminal_redraw_replaces_old_choice(monkeypatch: pytest.MonkeyPatch):
    view = InlineTerminal(lambda _key: None, columns=40, rows=4)
    monkeypatch.setattr(view, "_paint", lambda: None)
    view.feed("Choice: No\r\x1b[2KChoice: Yes")
    snapshot = view.snapshot()
    assert "Choice: Yes" in snapshot
    assert "Choice: No" not in snapshot


def test_mouse_wheel_scrolls_history_without_changing_live_screen(
    monkeypatch: pytest.MonkeyPatch,
):
    view = InlineTerminal(lambda _key: None, columns=20, rows=4)
    painted: list[str] = []
    monkeypatch.setattr(view, "update", lambda content: painted.append(content.plain))
    view.feed("\r\n".join(f"row-{n}" for n in range(8)))
    live = view.snapshot()
    assert "row-7" in live
    assert "row-0" not in live

    up = Mock()
    view.on_mouse_scroll_up(up)
    up.stop.assert_called_once()
    assert "row-1" in painted[-1]
    assert "row-7" not in painted[-1]
    assert view.snapshot() == live

    view.feed("\r\nrow-8")
    assert "row-1" in painted[-1], "new output must not snap scrollback to the bottom"

    down = Mock()
    view.on_mouse_scroll_down(down)
    down.stop.assert_called_once()
    view.on_mouse_scroll_down(Mock())
    assert "row-8" in painted[-1]


def test_mouse_wheel_without_history_can_scroll_outer_transcript(
    monkeypatch: pytest.MonkeyPatch,
):
    view = InlineTerminal(lambda _key: None, columns=20, rows=4)
    monkeypatch.setattr(view, "update", lambda _content: None)
    view.feed("short")
    event = Mock()
    view.on_mouse_scroll_up(event)
    event.stop.assert_not_called()


def test_wheel_stays_in_terminal_at_history_boundaries(monkeypatch: pytest.MonkeyPatch):
    view = InlineTerminal(lambda _key: None, columns=20, rows=4)
    monkeypatch.setattr(view, "update", lambda _content: None)
    view.feed("\r\n".join(f"row-{n}" for n in range(8)))
    view.on_mouse_scroll_up(Mock())
    view.on_mouse_scroll_up(Mock())
    at_top = Mock()
    view.on_mouse_scroll_up(at_top)
    at_top.stop.assert_called_once()

    view.on_mouse_scroll_down(Mock())
    view.on_mouse_scroll_down(Mock())
    at_live = Mock()
    view.on_mouse_scroll_down(at_live)
    at_live.stop.assert_called_once()


def test_scrollback_stays_on_same_line_after_history_fills(
    monkeypatch: pytest.MonkeyPatch,
):
    view = InlineTerminal(lambda _key: None, columns=20, rows=4)
    painted: list[str] = []
    monkeypatch.setattr(view, "update", lambda content: painted.append(content.plain))
    view.feed("\r\n".join(f"row-{n:04}" for n in range(1603)))
    assert len(view._screen.history.top) == 1000
    view.on_mouse_scroll_up(Mock())
    before = painted[-1]
    assert "row-1599" in before

    view.feed("\r\nrow-1603")
    assert painted[-1] == before


def test_view_change_reports_live_and_history(monkeypatch: pytest.MonkeyPatch):
    positions: list[tuple[int, int]] = []
    view = InlineTerminal(
        lambda _key: None, columns=20, rows=4, on_view_change=lambda *pos: positions.append(pos)
    )
    monkeypatch.setattr(view, "update", lambda _content: None)
    view.feed("\r\n".join(f"row-{n}" for n in range(8)))
    assert positions[-1] == view.view_position == (0, 4)
    view.on_mouse_scroll_up(Mock())
    assert positions[-1] == (3, 4)
    view.feed("\r\nrow-8")
    assert positions[-1] == (4, 5)


@pytest.mark.asyncio
async def test_textual_routes_wheel_event_to_embedded_terminal():
    terminal = InlineTerminal(lambda _key: None, columns=20, rows=4)

    class TerminalApp(App):
        def compose(self) -> ComposeResult:
            yield terminal

    async with TerminalApp().run_test() as pilot:
        terminal.feed("\r\n".join(f"row-{n}" for n in range(8)))
        terminal.post_message(
            MouseScrollUp(
                widget=terminal,
                x=0,
                y=0,
                delta_x=0,
                delta_y=-1,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
            )
        )
        await pilot.pause()
        assert terminal._scroll_offset == 3


@pytest.mark.asyncio
async def test_terminal_screen_forwards_keys_and_returns_to_chat():
    from textual.widgets import Static

    sent: list[str] = []
    terminal = InlineTerminal(sent.append, return_to_chat=lambda: None)

    class TerminalApp(App):
        def compose(self) -> ComposeResult:
            yield Static("chat")

    async with TerminalApp().run_test() as pilot:
        pilot.app.push_screen(TerminalCommandScreen(Static("command"), terminal, Static("hint")))
        await pilot.pause()
        assert terminal.has_focus
        await pilot.press("down")
        assert sent == ["\x1b[B"]
