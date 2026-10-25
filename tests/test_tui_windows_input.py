"""VM helpers cannot leave an active Windows TUI without virtual-terminal input."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from novacode_cli.tui.app import NovaApp

Terminal = tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch):
    import textual.drivers

    class WindowsDriver(SimpleNamespace):
        is_headless = False
        _event_thread = object()
        _enable_mouse_support = Mock()

    mode = SimpleNamespace(value=0x7)

    def set_mode(_stream: object, value: int) -> bool:
        mode.value = value
        return True

    win32 = SimpleNamespace(
        ENABLE_VIRTUAL_TERMINAL_INPUT=0x200,
        get_console_mode=Mock(side_effect=lambda _stream: mode.value),
        set_console_mode=Mock(side_effect=set_mode),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(textual.drivers, "win32", win32, raising=False)
    monkeypatch.setitem(
        sys.modules, "textual.drivers.windows_driver", SimpleNamespace(WindowsDriver=WindowsDriver)
    )
    return WindowsDriver(), win32, mode


def test_helper_overwritten_input_mode_is_repaired_once(terminal: Terminal):
    from novacode_cli.tui.windows_input import restore_input_mode

    driver, win32, mode = terminal
    restore_input_mode(driver)
    assert mode.value == 0x200
    win32.set_console_mode.assert_called_once_with(sys.__stdin__, 0x200)
    driver._enable_mouse_support.assert_called_once()
    restore_input_mode(driver)
    win32.set_console_mode.assert_called_once()
    driver._enable_mouse_support.assert_called_once()


@pytest.mark.parametrize("state", ["headless", "suspended", "other-driver", "non-windows"])
def test_inactive_or_different_drivers_are_untouched(
    terminal: Terminal, monkeypatch: pytest.MonkeyPatch, state: str
):
    from novacode_cli.tui.windows_input import restore_input_mode

    driver, win32, _mode = terminal
    if state == "headless":
        driver.is_headless = True
    elif state == "suspended":
        driver._event_thread = None
    elif state == "other-driver":
        driver = SimpleNamespace(is_headless=False)
    else:
        monkeypatch.setattr(sys, "platform", "linux")
    restore_input_mode(driver)
    win32.get_console_mode.assert_not_called()
    win32.set_console_mode.assert_not_called()


def test_failed_console_access_does_not_break_tui(terminal: Terminal):
    from novacode_cli.tui.windows_input import restore_input_mode

    driver, win32, _mode = terminal
    win32.get_console_mode.side_effect = OSError("console is closing")
    restore_input_mode(driver)
    win32.set_console_mode.assert_not_called()
    driver._enable_mouse_support.assert_not_called()


def test_existing_status_tick_repairs_input_before_refreshing(terminal: Terminal):
    driver, _win32, mode = terminal
    seen = []
    app = SimpleNamespace(
        _driver=driver, _tick=lambda: seen.append(mode.value), _schedule_status_tick=Mock()
    )
    NovaApp._status_tick(app)
    assert seen == [0x200]
    app._schedule_status_tick.assert_called_once()


def test_returning_focus_repairs_input(terminal: Terminal):
    driver, _win32, mode = terminal
    app = SimpleNamespace(_driver=driver, _schedule_status_tick=Mock(), _matrix_rain=lambda: None)
    NovaApp.on_app_focus(app)
    assert mode.value == 0x200
    assert app._os_focused


def test_failed_mode_change_does_not_emit_mouse_sequences(terminal: Terminal):
    from novacode_cli.tui.windows_input import restore_input_mode

    driver, win32, _mode = terminal
    win32.set_console_mode.side_effect = None
    win32.set_console_mode.return_value = False
    restore_input_mode(driver)
    driver._enable_mouse_support.assert_not_called()
