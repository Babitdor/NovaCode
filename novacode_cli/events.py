"""Shared event buffer for Nova middleware events.

This module owns the module-level ``nova_event_log`` list that multiple
middleware modules (Hermes, Security) append to, and which the agent event
loop drains into ``ContextMessage`` UI events.

Extracted from ``hermes/middleware.py`` to break the implicit dependency:
SecurityMiddleware and memory_tiers must NOT depend on NovaLearningMiddleware
just to surface a TUI-safe notice.

Usage::

    from novacode_cli.events import nova_event_log

    nova_event_log.append(("nova_security", "⛨", "yellow", "Blocked spoofed domain"))

The list is cleared after each drain by ``iterate_agent_events`` in
``core/agent_loop.py``.
"""

from __future__ import annotations


_MAX_EVENT_LOG = 200  # Cap to prevent unbounded growth if drain stalls

# Module-level event buffer for Nova events (review cycles, skill activity,
# security notices, memory operations).
# The middleware appends ``(event_type, icon, color, message)`` tuples here, and
# ``iterate_agent_events`` in ``core/agent_loop.py`` drains them into proper
# :class:`~novacode_cli.ui_events.ContextMessage` events that both the Rich
# console renderer and the Textual TUI consume.
nova_event_log: list[tuple[str, str, str, str]] = []
"""``(event_type, icon, color, message)`` log drained by the agent event loop.

The list is cleared after each drain.  Event types:
- ``nova_review_start``
- ``nova_review_complete``
- ``nova_skill_refinement``
- ``nova_skill_created``
- ``nova_security``
- ``nova_memory``

Loop-Engineering event types (added with the verification / hill-climbing /
event-driven enhancements):
- ``nova_verification_retry``  — inline verifier sent feedback for a retry
- ``nova_verification_pass``   — output passed the rubric grader
- ``nova_verification_fail``   — output failed after retries were exhausted
- ``nova_threshold_tuned``     — auto-tuner adjusted a review threshold
- ``nova_prompt_evolved``      — a prompt-template candidate was proposed/promoted
- ``nova_cron_fired``          — a scheduled (heartbeat) task was enqueued
- ``nova_webhook_received``    — an external webhook produced a task
"""


def cap_event_log() -> None:
    """Trim the event log to ``_MAX_EVENT_LOG`` entries if it exceeds the limit.

    Called by append sites to prevent unbounded growth when the drain stalls.
    """
    if len(nova_event_log) > _MAX_EVENT_LOG:
        del nova_event_log[: len(nova_event_log) - _MAX_EVENT_LOG]


# --- Callback registry for live tool output streaming ---
import threading
from typing import Callable

_output_callbacks: list[Callable[[str, str], None]] = []
_callbacks_lock = threading.Lock()


def register_tool_output_callback(cb: Callable[[str, str], None]) -> None:
    """Register a callback to receive live tool stdout/stderr updates."""
    with _callbacks_lock:
        if cb not in _output_callbacks:
            _output_callbacks.append(cb)


def unregister_tool_output_callback(cb: Callable[[str, str], None]) -> None:
    """Unregister a live tool output callback."""
    with _callbacks_lock:
        if cb in _output_callbacks:
            _output_callbacks.remove(cb)


def emit_tool_output(call_id: str, text: str) -> None:
    """Emit a chunk of tool output to all registered callbacks."""
    with _callbacks_lock:
        callbacks = list(_output_callbacks)
    for cb in callbacks:
        try:
            cb(call_id, text)
        except Exception:
            pass
