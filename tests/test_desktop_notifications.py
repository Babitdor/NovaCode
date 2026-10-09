"""Native icon loading, callback routing, and bounded notification lifecycle."""

import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from novacode_cli.desktop_notifications import DesktopNotifier, ICON_PATH, icon_resource


def test_packaged_icon_is_the_requested_nova_png():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    assert ICON_PATH.read_bytes() == (root / "Nova.png").read_bytes()
    config = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    assert "assets/Nova.png" in config["tool"]["setuptools"]["package-data"]["novacode_cli"]
    assert len(icon_resource()) > 40


def test_click_focuses_owner_then_routes_exact_session_and_notification():
    calls = []
    native = Mock()
    native.focus.side_effect = lambda window: calls.append(("focus", window))
    notifier = DesktopNotifier(lambda sid, nid: calls.append((sid, nid)), native=native)
    notifier.window = (123, 456)
    notifier._clicked("child-2", "note-9")
    assert calls == [("focus", (123, 456)), ("child-2", "note-9")]
    notifier.close()
    notifier._clicked("root", "stale")
    assert len(calls) == 2


def test_bounded_queue_and_closed_notifications_are_ignored():
    notifier = DesktopNotifier(Mock(), native=Mock())
    for index in range(100):
        notifier.show("root", str(index), "title", "message")
    assert notifier.commands.qsize() == 64
    notifier.close()
    notifier.show("root", "late", "title", "message")
    assert notifier.commands.qsize() == 64


def test_notification_thread_cleanup_and_click_binding(monkeypatch):
    monkeypatch.setattr("novacode_cli.desktop_notifications.sys.platform", "win32")
    stop = threading.Event()
    native = Mock()
    native.capture_terminal.return_value = (123, 456)
    native.run.side_effect = lambda queue: stop.wait(2)
    native.stop.side_effect = stop.set
    notifier = DesktopNotifier(Mock(), native=native)
    notifier.start()
    assert notifier.ready.wait(2)
    notifier.show("child", "note", "title", "message")
    notifier.close()
    assert not notifier.thread.is_alive()
    native.close.assert_called_once()
    assert native.open.call_args.args[1] == notifier._clicked


def test_native_start_failure_cleans_up(monkeypatch):
    monkeypatch.setattr("novacode_cli.desktop_notifications.sys.platform", "win32")
    native = Mock()
    native.open.side_effect = OSError("unavailable")
    notifier = DesktopNotifier(Mock(), native=native)
    notifier.start()
    notifier.thread.join(2)
    assert notifier.closed.is_set()
    native.close.assert_called_once()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows smoke test")
def test_real_windows_icon_and_hidden_callback_window():
    """Exercise native APIs without showing a balloon or changing focus."""
    from novacode_cli.desktop_notifications import _WindowsShell

    native = _WindowsShell()
    clicked = []
    try:
        native.open(icon_resource(), lambda sid, nid: clicked.append((sid, nid)))
        assert native.user.IsWindow(native.hwnd)
        assert native.icon
        native.items[5] = ("child", "note")
        native._message(native.hwnd, native.CALLBACK, 5, 0x405)
        assert clicked == [("child", "note")]
        assert 5 not in native.items
        native._message(native.hwnd, native.CALLBACK, 5, 0x405)
        assert len(clicked) == 1
    finally:
        native.close()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows handle validation")
def test_reused_window_handle_does_not_focus_another_process():
    from novacode_cli.desktop_notifications import _WindowsShell

    native = _WindowsShell()
    fake = Mock()
    fake.IsWindow.return_value = True
    fake.GetWindowThreadProcessId.side_effect = lambda hwnd, pid: setattr(pid._obj, "value", 999)
    native.user = fake
    native.focus((123, 456))
    fake.ShowWindow.assert_not_called()
    fake.SetForegroundWindow.assert_not_called()
