"""UI-agnostic agent streaming.

Thin wrapper around :func:`novacode_cli.core.agent_loop.iterate_agent_events`
that preserves the public ``run_agent_stream`` API for backward compatibility.

Human-in-the-loop interrupts are surfaced as :class:`~novacode_cli.ui_events.InterruptRequest`
events carrying an ``asyncio.Future`` that the consumer must resolve with the
decision; the generator awaits it and resumes the graph.

When the learning loop is enabled, each completed turn is graded out-of-band and
the verdict is logged — that verdict is the quality signal ``/prompt`` evolution
(prompt-template A/B testing) and ``verification_log`` feed on. Historically only
the legacy CLI path (``ui/execution.py`` via ``run_with_verification``) produced
this signal, so the TUI — which calls ``iterate_agent_events`` through here —
generated nothing, leaving ``verification_log`` empty and auto prompt-evolution
starved. The grade runs as a fire-and-forget background task: no retries and no
UI events, so it never delays or alters the turn.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from novacode_cli import ui_events as ev
from novacode_cli.core.autonomous_loop import run_with_goal
from novacode_cli.sessions.lease import lease_holder, lease_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

# Hold references to in-flight grade tasks so they aren't GC'd mid-run.
_verification_tasks: set[asyncio.Task] = set()


class _NullAsyncCM:
    """A no-op async context manager (used when there is no thread to lease)."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


def _null_async_cm() -> _NullAsyncCM:
    return _NullAsyncCM()


def _maybe_verifier() -> Any | None:
    """An ``InlineVerifier`` when the learning loop is enabled, else ``None``.

    Gated behind ``learning_enabled`` (opt-in) because grading adds one
    out-of-band model call per turn. Best-effort — any setup failure returns
    ``None`` and the turn runs unverified.
    """
    try:
        from novacode_cli.config.nova_config import NovaConfig

        if not NovaConfig().get_learning_enabled():
            return None
        from novacode_cli.hermes.verifier import InlineVerifier
        from novacode_cli.memory.store import get_durable_store

        return InlineVerifier(get_durable_store(), enabled=True)
    except Exception:  # noqa: BLE001 — learning is best-effort, never break a turn
        return None


def _spawn_verification_signal(
    verifier: Any,
    user_input: str,
    agent_output: str,
    file_ops: list,
    session_state: Any,
    tool_results: list | None = None,
) -> None:
    """Grade the finished turn and log the verdict (verification_log + prompt A/B).

    Fire-and-forget: no retries, no UI events. Skipped for a pure no-op turn
    (nothing produced to grade).
    """
    if verifier is None or (not agent_output and not file_ops):
        return

    async def _grade() -> None:
        try:
            from novacode_cli.core.verification_loop import (
                _extract_diffs,
                _extract_test_evidence,
                _record_prompt_ab_outcome,
            )

            test_evidence = _extract_test_evidence(tool_results or [])
            diffs = _extract_diffs(file_ops)
            verdict = await verifier.grade(
                user_input,
                agent_output,
                file_ops,
                test_evidence=test_evidence,
                diffs=diffs,
            )
            await verifier.log_outcome(getattr(session_state, "thread_id", ""), verdict, 0)
            # Feed prompt-evolution (Enhancement 2) its quality signal.
            await _record_prompt_ab_outcome(passed=verdict.passed)
        except Exception:  # noqa: BLE001 — a grading failure must never surface
            pass

    task = asyncio.create_task(_grade())
    _verification_tasks.add(task)
    task.add_done_callback(_verification_tasks.discard)


async def run_agent_stream(
    user_input: str,
    agent,
    assistant_id: str | None,
    session_state,
    *,
    backend=None,
    image_tracker=None,
    seen_message_ids: set[str] | None = None,
    skip_file_mentions: bool = False,
    can_continue: Callable[[], bool] | None = None,
) -> AsyncIterator[Any]:
    """Run the agent and yield UI events.

    Delegates to :func:`~novacode_cli.core.agent_loop.iterate_agent_events` (or
    :func:`~novacode_cli.core.autonomous_loop.run_with_goal` when a goal is
    active, so goal mode works from the TUI). Yields instances from
    :mod:`novacode_cli.ui_events`. Terminates with a
    :class:`~novacode_cli.ui_events.Done`, :class:`~novacode_cli.ui_events.Cancelled`,
    or :class:`~novacode_cli.ui_events.Error` event. When learning is enabled,
    grades the completed turn out-of-band (see module docstring).

    The turn runs under a best-effort cross-process session lease (never blocks;
    proceeds unleased on conflict).
    """
    verifier = _maybe_verifier()
    texts: list[str] = []
    file_ops: list[Any] = []
    tool_results: list[Any] = []

    thread_id = getattr(session_state, "thread_id", None) or getattr(
        session_state, "session_id", None
    )
    holder = lease_holder("tui")

    # Best-effort cross-process lease. Only acquired when we have a real
    # thread_id; never blocks (proceeds unleased on conflict).
    _lease_cm = lease_session(thread_id, holder) if thread_id else _null_async_cm()

    async with _lease_cm:
        async for event in run_with_goal(
            user_input,
            agent,
            assistant_id,
            session_state,
            backend=backend,
            image_tracker=image_tracker,
            seen_message_ids=seen_message_ids,
            skip_file_mentions=skip_file_mentions,
            can_continue=can_continue,
        ):
            if verifier is not None:
                if isinstance(event, ev.AssistantMessage):
                    texts.append(event.text)
                elif isinstance(event, ev.FileOp) and getattr(event, "record", None) is not None:
                    file_ops.append(event.record)
                elif isinstance(event, ev.ToolResult):
                    tool_results.append(event)
            yield event

    if verifier is not None:
        agent_output = "\n\n".join(t for t in texts if t).strip()
        _spawn_verification_signal(
            verifier, user_input, agent_output, file_ops, session_state, tool_results
        )
