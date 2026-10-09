"""Tests for the notification system."""

import asyncio

from novacode_cli.commands.notifications_handler import handle_notifications_command
from novacode_cli.states.Session import Notification, SessionState
from novacode_cli.states.slices.notifications import NotificationState


def test_add_notification():
    s = SessionState()
    nid = s.add_notification("info", "Test title", "Test message", "test")
    assert len(s.notifications) == 1
    assert s.unread_notification_count() == 1
    assert s.notifications[0].id == nid
    assert s.notifications[0].level == "info"
    assert s.notifications[0].title == "Test title"
    assert isinstance(s.notifications[0], Notification)


def test_hook_uses_current_icon_and_defers_to_tui_delivery(monkeypatch):
    from unittest.mock import Mock
    from novacode_cli.desktop_notifications import ICON_PATH

    dispatch = Mock()
    monkeypatch.setattr("novacode_cli.hooks.dispatch_hook_fire_and_forget", dispatch)
    state = SessionState()
    callback = Mock()
    state._notification_callback = callback
    state.add_notification("approval", "Approve", "Details", "test")
    callback.assert_called_once()
    payload = dispatch.call_args.args[1]
    assert payload["desktop_managed"] is True
    assert payload["icon_path"] == str(ICON_PATH.resolve())
    callback.return_value = False
    state.add_notification("info", "Fallback", "Details", "test")
    assert dispatch.call_args.args[1]["desktop_managed"] is False
    callback.side_effect = RuntimeError("delivery failed")
    state.add_notification("info", "Fallback", "Details", "test")
    assert dispatch.call_args.args[1]["desktop_managed"] is False


def test_legacy_windows_hook_skips_tui_notification_without_display():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell unavailable")
    script = Path(__file__).resolve().parents[1] / "examples/hooks/windows-notify.ps1"
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
        input=json.dumps({"desktop_managed": True}),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert not result.stdout and not result.stderr


def test_dismiss_notification():
    s = SessionState()
    nid = s.add_notification("warning", "Test", "Test", "test")
    assert s.dismiss_notification(nid) is True
    assert s.notifications[0].dismissed is True
    assert s.unread_notification_count() == 0


def test_dismiss_nonexistent():
    s = SessionState()
    assert s.dismiss_notification("nope") is False


def test_notification_queue_maxlen():
    s = SessionState()
    for i in range(110):
        s.add_notification("info", str(i), "", "test")
    assert len(s.notifications) == 100  # bounded
    # newest is appended left; oldest retained is "10"
    assert s.notifications[0].title == "109"
    assert s.notifications[-1].title == "10"


def test_clear_notifications():
    s = SessionState()
    for i in range(10):
        s.add_notification("info", str(i), "", "test")
    assert s.clear_notifications() == 10
    assert len(s.notifications) == 0
    assert s.unread_notification_count() == 0


def test_unread_excludes_dismissed():
    s = SessionState()
    a = s.add_notification("info", "a", "", "test")
    s.add_notification("error", "b", "", "test")
    assert s.unread_notification_count() == 2
    s.dismiss_notification(a)
    assert s.unread_notification_count() == 1


def test_handler_list_dismiss_clear():
    s = SessionState()
    nid = s.add_notification("error", "Boom", "details", "tests")
    # list (default)
    assert asyncio.run(handle_notifications_command(s, None)) is True
    # dismiss
    assert asyncio.run(handle_notifications_command(s, f"dismiss {nid}")) is True
    assert s.notifications[0].dismissed is True
    # clear
    assert asyncio.run(handle_notifications_command(s, "clear")) is True
    assert len(s.notifications) == 0


# -- direct NotificationState slice tests (independent of SessionState) --


def test_notification_state_add():
    """NotificationState slice can be tested without SessionState."""
    ns = NotificationState()
    nid = ns.add("info", "slice test", "direct", "unit")
    assert ns.unread_count() == 1
    assert ns.notifications[0].id == nid
    assert ns.notifications[0].level == "info"


def test_notification_state_dismiss():
    ns = NotificationState()
    nid = ns.add("warning", "dismiss me", "", "unit")
    assert ns.dismiss(nid) is True
    assert ns.unread_count() == 0
    assert ns.dismiss("nope") is False


def test_notification_state_clear():
    ns = NotificationState()
    for i in range(5):
        ns.add("info", str(i), "", "unit")
    assert ns.clear() == 5
    assert ns.unread_count() == 0


def test_notification_state_bounded():
    ns = NotificationState()
    for i in range(110):
        ns.add("info", str(i), "", "unit")
    assert len(ns.notifications) == 100
