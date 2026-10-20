"""Agent-to-agent mailbox — a lightweight in-process message bus.

Nova's subagents run synchronously (one at a time, via the `task` tool), so they
can't hold a live conversation. This mailbox gives peer messaging instead: any
agent (orchestrator or subagent) drops a message into another agent's inbox by
name, and reads its own inbox when it runs. The orchestrator sequences the turns
(dispatch A → A messages B → dispatch B → B reads + replies → A reads).

Process-global and thread-safe: subagents run in the same process as the
orchestrator, so a module-level store is all that's needed. It is NOT durable
across restarts (a conversation lives within one session), by design.
"""

from __future__ import annotations

import threading
import time

from langchain.tools import tool

_lock = threading.RLock()
# recipient agent name -> list of {"from": str, "text": str, "ts": float}
_mailbox: dict[str, list[dict]] = {}
_scoped_mailbox: dict[tuple[str, str, str, str], list[dict]] = {}
_MAX_PER_INBOX = 100  # cap so a runaway loop can't grow memory unbounded


@tool
def send_agent_message(to_agent: str, message: str, from_agent: str = "orchestrator") -> str:
    """Send a message to another agent's inbox (agent-to-agent communication).

    Use this to hand a question, result, or instruction to a specific peer agent
    by name. The recipient reads it with `read_agent_messages` when it next runs.

    Args:
        to_agent: The exact name of the recipient agent (e.g. "code-explorer").
        message: The message to deliver.
        from_agent: Your own agent name, so the recipient knows who wrote it.

    Returns:
        Confirmation that the message was queued.
    """
    to_agent = (to_agent or "").strip()
    if not to_agent:
        return "Error: to_agent is required."
    from novacode_cli.teams.legacy import mailbox_identity

    scope = mailbox_identity.get()
    if scope is not None:
        from_agent = scope[3]
    with _lock:
        inbox = (
            _scoped_mailbox.setdefault((*scope[:3], to_agent), [])
            if scope
            else _mailbox.setdefault(to_agent, [])
        )
        if scope and (len(inbox) >= _MAX_PER_INBOX or len(message.encode()) > 16384):
            return "Error: scoped inbox limit reached; read pending messages first."
        inbox.append({"from": from_agent or "unknown", "text": message, "ts": time.time()})
        if len(inbox) > _MAX_PER_INBOX:
            del inbox[: len(inbox) - _MAX_PER_INBOX]
    return f"Message delivered to '{to_agent}' inbox."


@tool
def read_agent_messages(as_agent: str, clear: bool = True) -> str:
    """Read messages addressed to you (agent-to-agent communication).

    Args:
        as_agent: Your own agent name — the inbox to read.
        clear: When True (default), consume the messages so they aren't re-read.

    Returns:
        The messages addressed to you, newest last, or "No messages.".
    """
    as_agent = (as_agent or "").strip()
    if not as_agent:
        return "Error: as_agent is required (pass your own agent name)."
    from novacode_cli.teams.legacy import mailbox_identity

    scope = mailbox_identity.get()
    with _lock:
        if scope:
            if as_agent != scope[3]:
                return "Error: cannot read another agent's inbox."
            msgs = list(_scoped_mailbox.get(scope, []))
            if clear:
                _scoped_mailbox.pop(scope, None)
        else:
            msgs = list(_mailbox.get(as_agent, []))
            if clear:
                _mailbox[as_agent] = []
    if not msgs:
        return "No messages."
    return "\n".join(f"[from {m['from']}] {m['text']}" for m in msgs)


def clear_mailbox() -> None:
    """Drop every inbox (used on /clear so a new conversation starts empty)."""
    with _lock:
        _mailbox.clear()


if __name__ == "__main__":
    clear_mailbox()
    assert send_agent_message.invoke({"to_agent": "b", "message": "hi B", "from_agent": "a"}).startswith(
        "Message delivered"
    )
    assert read_agent_messages.invoke({"as_agent": "b"}) == "[from a] hi B"
    assert read_agent_messages.invoke({"as_agent": "b"}) == "No messages."  # cleared
    assert read_agent_messages.invoke({"as_agent": "nobody"}) == "No messages."
    print("agent_mailbox self-check ok")


def clear_scoped_mailbox(project: str, root: str) -> None:
    """Release one root's legacy inboxes after its owned workers stop."""
    with _lock:
        for key in list(_scoped_mailbox):
            if key[:2] == (project, root):
                _scoped_mailbox.pop(key, None)
