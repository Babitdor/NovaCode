"""Agent-to-agent mailbox: peer messaging between agents by name."""

from __future__ import annotations

from novacode_cli.agents.agent_mailbox import (
    clear_mailbox,
    read_agent_messages,
    send_agent_message,
)


def _send(to, msg, frm="orchestrator"):
    return send_agent_message.invoke({"to_agent": to, "message": msg, "from_agent": frm})


def _read(who, clear=True):
    return read_agent_messages.invoke({"as_agent": who, "clear": clear})


def test_message_delivered_and_read():
    clear_mailbox()
    assert _send("code-explorer", "please review PR #42", "bug-fix-agent").startswith(
        "Message delivered"
    )
    out = _read("code-explorer")
    assert "[from bug-fix-agent] please review PR #42" == out


def test_read_clears_by_default():
    clear_mailbox()
    _send("b", "hi")
    assert _read("b") != "No messages."
    assert _read("b") == "No messages."  # consumed


def test_read_without_clear_keeps_messages():
    clear_mailbox()
    _send("b", "keep me")
    assert _read("b", clear=False) != "No messages."
    assert _read("b", clear=False) != "No messages."  # still there


def test_inboxes_are_isolated_and_ordered():
    clear_mailbox()
    _send("a", "first", "x")
    _send("a", "second", "y")
    _send("b", "for b only", "x")
    a = _read("a")
    assert a == "[from x] first\n[from y] second"  # order preserved
    assert _read("b") == "[from x] for b only"  # isolated per recipient


def test_missing_recipient_and_empty_name():
    clear_mailbox()
    assert _read("nobody") == "No messages."
    assert "required" in send_agent_message.invoke(
        {"to_agent": "", "message": "x"}
    )
    assert "required" in read_agent_messages.invoke({"as_agent": ""})
