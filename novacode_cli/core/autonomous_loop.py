"""Autonomous goal loop — wraps ``iterate_agent_events`` (goal mode).

:func:`run_with_goal` is a drop-in for
:func:`~novacode_cli.core.agent_loop.iterate_agent_events`: it yields the *same*
UI events, but when ``session_state.active_goal`` is set it keeps re-driving the
agent across turns until the goal is reported achieved with evidence, paused,
the turn budget is exhausted, or the goal is cleared.

The kickoff/follow-up prompts (``commands/side_commands.py``) instruct the agent
to emit ``GOAL ACHIEVED`` with an evidence line once the goal is verifiably done;
this wrapper checks the declaration in each turn's assistant text. ``Done`` is
withheld until the run actually ends (same contract as
:func:`~novacode_cli.core.verification_loop.run_with_verification`), so the
front-ends see one terminal event. ``Cancelled`` / ``Error`` pass straight
through and end the run.

When ``verify`` is set, each turn is routed through
:func:`~novacode_cli.core.verification_loop.run_with_verification` so the rubric
grader still gates every autonomous turn.

It wraps the canonical generator from *outside* rather than implementing an
``AgentMiddleware`` — so it sidesteps the sync/async ``wrap_*`` contract entirely
and keeps ``iterate_agent_events`` (and therefore both front-ends) untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from novacode_cli import ui_events as ev
from novacode_cli.commands.side_commands import (
    DEFAULT_GOAL_MAX_TURNS,
    build_goal_followup,
    goal_achieved,
)
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.tracking.usage_tree import scoped_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = logging.getLogger("nova.core.autonomous_loop")


async def run_with_goal(  # noqa: PLR0911, PLR0912, PLR0915
    user_input: str,
    agent: object,
    assistant_id: str | None,
    session_state: Any,  # noqa: ANN401 — includes minimal session proxies
    *,
    backend: object = None,
    image_tracker: object = None,
    seen_message_ids: set[str] | None = None,
    skip_file_mentions: bool = False,
    max_turns: int | None = None,
    verify: bool = False,
    can_continue: Callable[[], bool] | None = None,
) -> AsyncIterator[Any]:
    """Run the agent autonomously toward the active goal, bounded by a turn cap.

    With no active goal this is a zero-overhead passthrough to
    :func:`iterate_agent_events`. With a goal, each turn is followed by a
    continuation prompt (unless the agent reported ``GOAL ACHIEVED``, the turn
    budget is exhausted, or the goal was cleared mid-run).

    Yields the same events as :func:`iterate_agent_events`. ``Done`` is withheld
    until the run ends; ``Cancelled`` / ``Error`` pass straight through.
    """
    goal = getattr(session_state, "active_goal", None)
    status = getattr(session_state, "goal_status", "active")
    thread_id = getattr(session_state, "thread_id", None)
    goal_thread_id = getattr(session_state, "goal_thread_id", thread_id)
    if (
        not goal
        or status != "active"
        or goal_thread_id != thread_id
        or getattr(session_state, "plan_mode_enabled", False)
    ):
        async for event in iterate_agent_events(
            user_input,
            agent,
            assistant_id,
            session_state,
            backend=backend,
            image_tracker=image_tracker,
            seen_message_ids=seen_message_ids,
            skip_file_mentions=skip_file_mentions,
        ):
            yield event
        return

    limit: int = (
        max_turns
        if max_turns is not None
        else getattr(session_state, "goal_max_turns", DEFAULT_GOAL_MAX_TURNS)
    )
    turn = getattr(session_state, "goal_turns_used", 0)
    if turn >= limit:
        session_state.goal_status = "budget_limited"
        yield ev.ContextMessage(
            message=f"Goal budget reached ({turn}/{limit} turns). Use /goal status or "
            "/goal budget <turns> to extend it.",
            event_type="nova_goal_finish",
            icon="🎯",
            color="yellow",
        )
        yield ev.Done()
        return
    start_turn = turn
    current_input = user_input
    run_revision = getattr(session_state, "goal_revision", None)

    def continuation_allowed() -> bool:
        queue = getattr(session_state, "_remote_message_queue", None)
        return (
            not getattr(session_state, "plan_mode_enabled", False)
            and (can_continue is None or can_continue())
            and (queue is None or queue.empty())
        )

    while True:
        turn += 1
        session_state.goal_turns_used = turn
        assistant_texts: list[str] = []
        done_event: ev.Done | None = None
        used_tool = False

        if verify:
            from novacode_cli.core.verification_loop import run_with_verification
            from novacode_cli.hermes.verifier import InlineVerifier
            from novacode_cli.memory.store import get_durable_store

            source = run_with_verification(
                current_input,
                agent,
                assistant_id,
                session_state,
                backend=backend,
                image_tracker=image_tracker,
                seen_message_ids=seen_message_ids,
                skip_file_mentions=skip_file_mentions,
                verifier=InlineVerifier(get_durable_store(), enabled=True),
            )
        else:
            source = iterate_agent_events(
                current_input,
                agent,
                assistant_id,
                session_state,
                backend=backend,
                image_tracker=image_tracker,
                seen_message_ids=seen_message_ids,
                skip_file_mentions=skip_file_mentions,
            )

        # Each autonomous turn is attributed to "goal" so the tree separates
        # the base turn from the autonomous re-drives.
        try:
            async for event in scoped_stream(source, "goal"):
                if isinstance(event, ev.AssistantMessage):
                    assistant_texts.append(event.text)
                    yield event
                elif isinstance(event, ev.ToolCall):
                    used_tool = True
                    yield event
                elif isinstance(event, ev.Done):
                    done_event = event  # held back pending the goal check
                elif isinstance(event, (ev.Cancelled, ev.Error)):
                    if getattr(session_state, "goal_revision", None) == run_revision:
                        session_state.goal_status = "paused"
                    yield event
                    return
                else:
                    yield event
        finally:
            # Worker cancellation and generator closure may bypass an explicit
            # Cancelled event. Preserve the objective for an explicit resume.
            if (
                done_event is None
                and getattr(session_state, "active_goal", None) == goal
                and getattr(session_state, "goal_revision", None) == run_revision
                and getattr(session_state, "goal_status", "active") == "active"
            ):
                session_state.goal_status = "paused"

        # Stream ended. No Done means Cancelled/Error already returned above.
        if done_event is None:
            return
        if getattr(session_state, "thread_id", None) != thread_id:
            yield done_event
            return

        # Re-read the goal each iteration so `/goal clear` stops the run.
        previous_goal = goal
        goal = getattr(session_state, "active_goal", None)
        status = getattr(session_state, "goal_status", "active")
        revision = getattr(session_state, "goal_revision", None)
        if goal and (revision != run_revision or goal != previous_goal):
            # A new objective arrived while the old turn was running. Its
            # output cannot complete the replacement goal or spend its budget.
            if (
                status == "active"
                and getattr(session_state, "thread_id", None) == thread_id
                and continuation_allowed()
            ):
                from novacode_cli.commands.side_commands import build_goal_kickoff

                run_revision = revision
                turn = getattr(session_state, "goal_turns_used", 0)
                start_turn = turn
                limit = (
                    max_turns
                    if max_turns is not None
                    else getattr(session_state, "goal_max_turns", DEFAULT_GOAL_MAX_TURNS)
                )
                current_input = build_goal_kickoff(goal)
                yield ev.ContextMessage(
                    message="Starting the updated goal.", event_type="nova_goal_continue", icon="🎯"
                )
                continue
            yield done_event
            return
        if max_turns is None:
            limit = getattr(session_state, "goal_max_turns", DEFAULT_GOAL_MAX_TURNS)
        agent_output = "\n\n".join(t for t in assistant_texts if t).strip()
        achieved = goal_achieved(agent_output) and continuation_allowed()
        if not goal or status != "active" or achieved or turn >= limit:
            if goal and status == "active":
                session_state.goal_status = "complete" if achieved else "budget_limited"
            # A single clean turn (achieved on turn 1) needs no notice; a
            # mid-run goal clear is an interruption and always gets one.
            if turn > start_turn + 1 or not goal or status != "active" or not achieved:
                if not goal:
                    message = "Autonomous goal run stopped — goal cleared."
                    color = "yellow"
                elif status == "paused":
                    message = "Autonomous goal run paused."
                    color = "yellow"
                elif achieved:
                    message = f"Autonomous goal run finished after {turn} turn(s)."
                    color = "green"
                else:
                    message = f"Goal budget reached after {turn}/{limit} turn(s)."
                    color = "yellow"
                yield ev.ContextMessage(
                    message=message,
                    event_type="nova_goal_finish",
                    icon="🎯",
                    color=color,
                )
            yield done_event
            return

        if not continuation_allowed():
            # Let the front-end drain queued input before any autonomous work.
            yield done_event
            return

        if turn > start_turn + 1 and not used_tool:
            yield ev.ContextMessage(
                message="Goal continuation stopped after a turn without tool work. "
                "The goal remains active; send a message to continue.",
                event_type="nova_goal_finish",
                icon="🎯",
                color="yellow",
            )
            yield done_event
            return

        yield ev.ContextMessage(
            message=f"Continuing toward goal — turn {turn}/{limit}.",
            event_type="nova_goal_continue",
            icon="🎯",
            color="cyan",
        )
        current_input = build_goal_followup(goal, turn, limit)
