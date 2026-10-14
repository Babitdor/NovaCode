"""Focusable terminal screen for commands running inside Nova's transcript."""

from __future__ import annotations

from itertools import islice
from typing import TYPE_CHECKING

import pyte
from rich.text import Text
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static
from wcwidth import wcwidth

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult
    from textual.events import Key, MouseScrollDown, MouseScrollUp

_SCROLLBACK_LINES = 1000
_WHEEL_LINES = 3
_WIDE_CELL_WIDTH = 2


_KEYS = {
    "enter": "\r",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "backspace": "\x7f",
    "delete": "\x1b[3~",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "tab": "\t",
    "escape": "\x1b",
    "space": " ",
}


class _TrackedHistoryScreen(pyte.HistoryScreen):
    """Count outgoing lines even after pyte's bounded history reaches capacity."""

    def __init__(self, columns: int, rows: int) -> None:
        self.line_advances = 0
        super().__init__(columns, rows, history=_SCROLLBACK_LINES)

    def index(self) -> None:
        bottom = self.margins[1] if self.margins else self.lines - 1
        if self.cursor.y == bottom:
            self.line_advances += 1
        super().index()


def terminal_key(key: str, character: str | None = None) -> str | None:
    """Translate Textual key names to bytes understood by a PTY."""
    if key in _KEYS:
        return _KEYS[key]
    if key.startswith("ctrl+"):
        letter = key.removeprefix("ctrl+")
        if len(letter) == 1 and letter.isalpha():
            return chr(ord(letter.lower()) - ord("a") + 1)
    if character and len(character) == 1:
        return character
    return key if len(key) == 1 else None


class InlineTerminal(Static):
    """Render a bounded VT screen and forward focused keys to its PTY."""

    can_focus = True

    def __init__(
        self,
        send: Callable[[str], None],
        *,
        columns: int = 100,
        rows: int = 16,
        on_view_change: Callable[[int, int], None] | None = None,
        return_to_chat: Callable[[], None] | None = None,
    ) -> None:
        """Create a fixed-size emulator and its key sink."""
        super().__init__(classes="bash-terminal")
        self._send = send
        self._on_view_change = on_view_change
        self._return_to_chat = return_to_chat
        self._screen = _TrackedHistoryScreen(columns, rows)
        self._stream = pyte.Stream(self._screen)
        self._scroll_offset = 0

    def on_mount(self) -> None:
        """Draw after Textual has attached the widget to an app."""
        self._paint()

    def feed(self, output: str) -> None:
        """Apply terminal control sequences before repainting the visible screen."""
        advances_before = self._screen.line_advances
        self._stream.feed(output)
        if self._scroll_offset:
            added = self._screen.line_advances - advances_before
            self._scroll_offset = min(len(self._screen.history.top), self._scroll_offset + added)
        self._paint()
        self._notify_view_change()

    def snapshot(self) -> str:
        """Return the latest readable terminal state for the agent log."""
        return "\n".join(row.rstrip() for row in self._screen.display).strip()

    @property
    def view_position(self) -> tuple[int, int]:
        """Return lines above live output and the available scrollback size."""
        return self._scroll_offset, len(self._screen.history.top)

    def _paint(self) -> None:
        visible = [row.rstrip() for row in self._screen.display]
        if self._scroll_offset:
            history_size = len(self._screen.history.top)
            end = history_size + len(visible) - self._scroll_offset
            start = max(0, end - self._screen.lines)
            saved = islice(
                self._screen.history.top,
                min(start, history_size),
                min(end, history_size),
            )
            visible = [self._history_line(line) for line in saved] + visible[
                max(0, start - history_size) : max(0, end - history_size)
            ]
        while visible and not visible[-1]:
            visible.pop()
        self.update(Text("\n".join(visible)))

    def _history_line(self, line: object) -> str:
        """Render pyte's saved cells with the same wide-character rules as its screen."""
        rendered: list[str] = []
        wide_stub = False
        for column in range(self._screen.columns):
            if wide_stub:
                wide_stub = False
                continue
            cell = line[column].data  # type: ignore[index]
            rendered.append(cell)
            wide_stub = bool(cell) and wcwidth(cell[0]) == _WIDE_CELL_WIDTH
        return "".join(rendered).rstrip()

    def _scroll(self, amount: int) -> bool:
        previous = self._scroll_offset
        self._scroll_offset = max(0, min(len(self._screen.history.top), previous + amount))
        if self._scroll_offset == previous:
            return False
        self._paint()
        self._notify_view_change()
        return True

    def _notify_view_change(self) -> None:
        if self._on_view_change is not None:
            self._on_view_change(self._scroll_offset, len(self._screen.history.top))

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        """Scroll this terminal's history before the outer transcript."""
        if self._scroll(_WHEEL_LINES) or self._screen.history.top:
            event.stop()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        """Return toward the live command screen with the mouse wheel."""
        if self._scroll(-_WHEEL_LINES) or self._screen.history.top:
            event.stop()

    def on_key(self, event: Key) -> None:
        """Keep Nova's own prompt independent while this terminal has focus."""
        if self._return_to_chat is not None and event.key in {"ctrl+g", "shift+tab"}:
            self._return_to_chat()
        elif event.key == "shift+tab":
            from novacode_cli.tui.widgets import PromptInput

            self.app.query_one("#prompt", PromptInput).focus()
        else:
            value = terminal_key(event.key, event.character)
            if value is not None:
                self._send(value)
        event.stop()
        event.prevent_default()

    def action_send_ctrl_c(self) -> None:
        """Pass Ctrl+C to the child while this terminal is focused."""
        self._send("\x03")


class TerminalCommandScreen(Screen):
    """Give an interactive command its own view inside Nova's TUI."""

    DEFAULT_CSS = """
    TerminalCommandScreen { background: $background; }
    TerminalCommandScreen > Vertical { height: 1fr; padding: 1 2; }
    TerminalCommandScreen .bash-terminal {
        height: 1fr; max-height: 100%; width: 1fr;
    }
    """

    def __init__(self, head: Static, terminal: InlineTerminal, hint: Static) -> None:
        """Keep the command display mounted until the user returns to chat."""
        super().__init__()
        self.terminal = terminal
        self._head = head
        self._hint = hint

    def compose(self) -> ComposeResult:
        """Arrange the terminal between its command row and status hint."""
        yield Vertical(self._head, self.terminal, self._hint)

    def on_mount(self) -> None:
        """Send new keys to the command as soon as the view opens."""
        self.terminal.focus()
