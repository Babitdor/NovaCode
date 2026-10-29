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

Each runtime session has its own queue, inherited by asynchronous tasks and
worker threads through ``current_identity``. Unscoped startup and scheduler
notices retain their process-wide delivery. The agent loop drains its session
explicitly, including while the graph is paused for approval.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterable, Iterator, MutableSequence
from typing import cast, overload

from novacode_cli.session.runtime_identity import current_identity

_MAX_EVENT_LOG = 200  # Cap to prevent unbounded growth if drain stalls


Event = tuple[str, str, str, str]


class _SessionEventLog(MutableSequence[Event]):
    """List-compatible, bounded queues owned by the producing runtime session."""

    def __init__(self) -> None:
        self._logs: dict[str | None, list[Event]] = {}
        self._lock = threading.RLock()

    def _log(self) -> list[Event]:
        identity = current_identity.get()
        return self._logs.setdefault(identity.session if identity else None, [])

    def __len__(self) -> int:
        with self._lock:
            return len(self._log())

    @overload
    def __getitem__(self, index: int) -> Event: ...

    @overload
    def __getitem__(self, index: slice) -> list[Event]: ...

    def __getitem__(self, index: int | slice) -> Event | list[Event]:
        with self._lock:
            return self._log()[index]

    @overload
    def __setitem__(self, index: int, value: Event) -> None: ...

    @overload
    def __setitem__(self, index: slice, value: Iterable[Event]) -> None: ...

    def __setitem__(self, index: int | slice, value: Event | Iterable[Event]) -> None:
        with self._lock:
            if isinstance(index, slice):
                self._log()[index] = cast("Iterable[Event]", value)
            else:
                self._log()[index] = cast("Event", value)

    def __delitem__(self, index: int | slice) -> None:
        with self._lock:
            del self._log()[index]

    def __iter__(self) -> Iterator[Event]:
        with self._lock:
            return iter(self._log().copy())

    def insert(self, index: int, value: Event) -> None:
        with self._lock:
            self._log().insert(index, value)
            self._cap()

    def append(self, value: Event) -> None:
        with self._lock:
            self._log().append(value)
            self._cap()

    def extend(self, values: Iterable[Event]) -> None:
        with self._lock:
            for value in values:
                self.append(value)

    def _cap(self) -> None:
        log = self._log()
        if len(log) > _MAX_EVENT_LOG:
            del log[: len(log) - _MAX_EVENT_LOG]

    def release_session(self, session: str) -> None:
        """Forget events belonging to a closed session."""
        with self._lock:
            self._logs.pop(session, None)

    def drain(self, session: str) -> list[Event]:
        """Atomically collect events for the consumer outside the graph context."""
        with self._lock:
            return self._logs.pop(None, []) + self._logs.pop(session, [])


# Module-level event buffer for Nova events (review cycles, skill activity,
# security notices, memory operations).
# The middleware appends ``(event_type, icon, color, message)`` tuples here, and
# ``iterate_agent_events`` in ``core/agent_loop.py`` drains them into proper
# :class:`~novacode_cli.ui_events.ContextMessage` events that both the Rich
# console renderer and the Textual TUI consume.
nova_event_log = _SessionEventLog()
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
        with contextlib.suppress(Exception):
            cb(call_id, text)
