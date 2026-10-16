"""Shared logic for the /goal and /btw commands.

These commands are UI-triggered but their behaviour must be identical across the
Textual TUI, the Rich REPL, and the remote bridge (Discord/Telegram). The pure
logic lives here so each front-end only handles rendering, not semantics — see
``CLAUDE.md`` on keeping cross-UI behaviour in shared code rather than per-renderer
copies (which drift).

- ``handle_goal_command`` mutates ``session_state.active_goal`` and reports what
  happened; ``active_goal`` is injected into every turn by
  :func:`novacode_cli.core.agent_loop.iterate_agent_events`.
- ``run_btw_question`` answers a side question on an ephemeral thread using a
  dedicated web-search agent, leaving the main conversation untouched.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_GOAL_MAX_TURNS",
    "GoalCommandResult",
    "build_goal_followup",
    "build_goal_kickoff",
    "goal_achieved",
    "goal_state_payload",
    "handle_goal_command",
    "is_goal_control",
    "restore_goal_state",
    "run_btw_question",
]

_GOAL_CLEAR_WORDS = frozenset({"clear", "off", "done", "stop"})

#: Default cap on autonomous goal-mode turns before the run stops.
DEFAULT_GOAL_MAX_TURNS = 5


def is_goal_control(text: str) -> bool:
    """Identify goal commands safe to apply while an agent turn is running."""
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].lower() != "/goal":
        return False
    args = parts[1].lower() if len(parts) > 1 else ""
    return args in {
        "",
        "status",
        "pause",
        "clear",
        "off",
        "done",
        "stop",
        "budget",
    } or args.startswith("budget ")


def goal_state_payload(session_state: Any) -> dict[str, Any] | None:  # noqa: ANN401
    """Serialize only the goal owned by the conversation being saved."""
    objective = getattr(session_state, "active_goal", None)
    thread = getattr(session_state, "thread_id", None)
    if not objective or getattr(session_state, "goal_thread_id", thread) != thread:
        return None
    return {
        "objective": objective,
        "rubric": getattr(session_state, "active_rubric", None),
        "status": getattr(session_state, "goal_status", "active"),
        "turns_used": getattr(session_state, "goal_turns_used", 0),
        "max_turns": getattr(session_state, "goal_max_turns", DEFAULT_GOAL_MAX_TURNS),
        "revision": getattr(session_state, "goal_revision", None),
    }


def goal_achieved(agent_output: str) -> bool:
    """Accept a completion declaration only when it includes an evidence line.

    The agent is instructed (in :func:`build_goal_kickoff`) to say
    ``**GOAL ACHIEVED**`` on its own line followed by ``Evidence: ...``.
    Requiring a line avoids treating a quoted/example marker as completion.
    """
    lines = []
    fence = None
    for line in (agent_output or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fence_marker = stripped[:3]
            if fence is None:
                fence = fence_marker
            elif fence == fence_marker:
                fence = None
        elif fence is None:
            lines.append(line)
    output = "\n".join(lines)
    marker = re.search(r"(?im)^\s*(?:\*\*)?GOAL ACHIEVED(?:\*\*)?\s*$", output)
    return bool(marker and re.search(r"(?im)^\s*Evidence:\s*\S", output[marker.end() :]))


def build_goal_followup(goal: str, turn: int, limit: int) -> str:
    """Build the continuation prompt for an autonomous goal turn after the first.

    The agent has already run one turn toward *goal*; this asks it to keep going,
    reminding it of the goal, the turn budget (with how many turns remain), and the
    achievement marker.
    """
    remaining = max(0, limit - turn)
    return (
        f"[GOAL CONTINUATION] Turn {turn}/{limit} — {remaining} turn(s) remaining.\n"
        f"[GOAL] {goal}\n\n"
        "Keep working toward the objective. Check concrete evidence before completion. "
        "If complete, put **GOAL ACHIEVED** on its own line followed by an "
        "Evidence: line citing files, tests, logs, or other checks. If blocked, "
        "explain the blocker and what would unlock it."
    )


def build_goal_kickoff(goal: str) -> str:
    """Build the autonomous kick-off prompt streamed when a goal is set."""
    return (
        f"[GOAL] {goal}\n\n"
        "You are now in goal mode. Work autonomously to achieve the goal above.\n"
        "1. Analyse what is needed and form a clear execution plan.\n"
        "2. Execute the plan step by step, using your tools as needed.\n"
        "3. After each step verify your progress against the goal.\n"
        "4. When the goal is fully and verifiably achieved, put **GOAL ACHIEVED** "
        "on its own line followed by an Evidence: line with concrete checks. "
        "If blocked, explain the blocker.\n"
        "Start now."
    )


@dataclass
class GoalCommandResult:
    """Outcome of a ``/goal`` invocation.

    Attributes:
        action: The lifecycle action or ``"usage"``.
        message: Plain-text summary suitable for any front-end to display.
        goal: The active goal after the command (``None`` once cleared).
        kickoff: When ``action == "set"``, the prompt a front-end should run to
            start autonomous execution; otherwise ``None``.
    """

    action: str
    message: str
    goal: str | None = None
    kickoff: str | None = None
    rubric: str | None = None


def _handle_goal_rubric(session_state: Any, rest: str) -> GoalCommandResult:  # noqa: ANN401
    """Set or clear the acceptance rubric (the `/goal rubric ...` subcommand)."""
    if not rest or rest.lower() in _GOAL_CLEAR_WORDS:
        session_state.active_rubric = None
        return GoalCommandResult(
            "rubric-clear",
            "📋 Rubric cleared.",
            goal=getattr(session_state, "active_goal", None),
        )
    session_state.active_rubric = rest
    return GoalCommandResult(
        "rubric-set",
        f"📋 Acceptance rubric set — the grader will hold work to:\n{rest}",
        goal=getattr(session_state, "active_goal", None),
        rubric=rest,
    )


def handle_goal_command(session_state: Any, args: str) -> GoalCommandResult:  # noqa: ANN401, PLR0911, PLR0912
    """Parse and apply a ``/goal`` command, mutating ``session_state.active_goal``.

    Args:
        session_state: The session state (its ``active_goal`` is read/written).
        args: Everything after ``/goal`` (already stripped of the command word).

    Returns:
        A :class:`GoalCommandResult` describing what happened.
    """
    stripped = args.strip()
    low = stripped.lower()

    # `/goal rubric <criteria>` — set the acceptance rubric the grader holds the
    # work to (paired with the goal). `/goal rubric clear` drops just the rubric.
    if low.startswith("rubric"):
        return _handle_goal_rubric(session_state, stripped[len("rubric") :].strip())

    if low in {"", "status"}:
        goal = getattr(session_state, "active_goal", None)
        rubric = getattr(session_state, "active_rubric", None)
        parts: list[str] = []
        if goal:
            state = getattr(session_state, "goal_status", "active")
            used = getattr(session_state, "goal_turns_used", 0)
            budget = getattr(session_state, "goal_max_turns", DEFAULT_GOAL_MAX_TURNS)
            parts.append(f"🎯 Goal ({state}, {used}/{budget} turns):\n{goal}")
        if rubric:
            parts.append(f"📋 Rubric:\n{rubric}")
        if parts:
            return GoalCommandResult("status", "\n\n".join(parts), goal=goal, rubric=rubric)
        return GoalCommandResult(
            "status",
            "No goal. Use /goal <objective> to start one.",
        )

    goal = getattr(session_state, "active_goal", None)
    if low == "pause":
        if not goal or getattr(session_state, "goal_status", "active") != "active":
            return GoalCommandResult("usage", "No active goal to pause.", goal=goal)
        session_state.goal_status = "paused"
        return GoalCommandResult("pause", "🎯 Goal paused.", goal=goal)

    if low == "resume":
        if not goal or getattr(session_state, "goal_status", "active") != "paused":
            return GoalCommandResult("usage", "No paused goal to resume.", goal=goal)
        session_state.goal_status = "active"
        return GoalCommandResult(
            "resume",
            "🎯 Goal resumed.",
            goal=goal,
            kickoff=build_goal_followup(
                goal,
                getattr(session_state, "goal_turns_used", 0),
                getattr(session_state, "goal_max_turns", DEFAULT_GOAL_MAX_TURNS),
            ),
        )

    if low == "budget" or low.startswith("budget "):
        value = stripped.partition(" ")[2].strip()
        if not value.isdecimal() or int(value) < 1:
            return GoalCommandResult("usage", "Usage: /goal budget <positive turns>", goal=goal)
        session_state.goal_max_turns = int(value)
        message = f"🎯 Goal budget: {value} turns."
        if (
            goal
            and getattr(session_state, "goal_status", None) == "budget_limited"
            and int(value) > getattr(session_state, "goal_turns_used", 0)
        ):
            session_state.goal_status = "paused"
            message += " Use /goal resume to continue."
        return GoalCommandResult("budget", message, goal=goal)

    if low in _GOAL_CLEAR_WORDS:
        session_state.active_goal = None
        session_state.active_rubric = None
        session_state.goal_status = None
        session_state.goal_turns_used = 0
        session_state.goal_thread_id = None
        session_state.goal_revision = None
        return GoalCommandResult("clear", "🎯 Goal and rubric cleared.")

    goal = args.strip()
    session_state.active_goal = goal
    session_state.goal_status = "active"
    session_state.goal_turns_used = 0
    session_state.goal_thread_id = getattr(session_state, "thread_id", None)
    session_state.goal_revision = uuid.uuid4().hex
    return GoalCommandResult(
        "set",
        f"🎯 Goal set:\n{goal}",
        goal=goal,
        kickoff=build_goal_kickoff(goal),
    )


def restore_goal_state(session_state: Any, saved: object) -> None:  # noqa: ANN401
    """Restore a saved goal onto its owning thread, ignoring malformed metadata."""
    if not isinstance(saved, dict) or not saved.get("objective"):
        return
    status = saved.get("status")
    if status not in {"active", "paused", "complete", "budget_limited"}:
        status = "paused"
    try:
        used = max(0, int(saved.get("turns_used", 0)))
        limit = max(1, int(saved.get("max_turns", DEFAULT_GOAL_MAX_TURNS)))
    except (TypeError, ValueError):
        return
    session_state.active_goal = str(saved["objective"])
    session_state.active_rubric = saved.get("rubric")
    session_state.goal_status = status
    session_state.goal_turns_used = used
    session_state.goal_max_turns = limit
    session_state.goal_thread_id = session_state.thread_id
    session_state.goal_revision = saved.get("revision") or uuid.uuid4().hex


# The web-search agent is created lazily and cached for the process: it is
# stateless across questions (each /btw uses a fresh thread_id) so one instance
# is safe to share between the TUI and the remote bridge.
_btw_agent: Any = None


def get_btw_agent() -> Any:  # noqa: ANN401
    """Return the cached /btw web-search agent, creating it on first use."""
    global _btw_agent  # process-wide singleton by design
    if _btw_agent is None:
        from novacode_cli.agents.btw_agent import create_btw_agent
        from novacode_cli.config.model_create import create_model

        _btw_agent, _ = create_btw_agent(create_model())
    return _btw_agent


async def run_btw_question(
    question: str,
    *,
    on_event: Any = None,  # noqa: ANN401
) -> str:
    """Answer a side question on an ephemeral thread, isolated from the main chat.

    Runs the dedicated web-search agent on a fresh ``thread_id`` so nothing
    touches the main conversation history. Returns the agent's answer text.

    Args:
        question: The side question to answer.
        on_event: Optional callback invoked with each streamed ui_event (used by
            front-ends that want live tool/typing feedback). May be ``None``.

    Returns:
        The answer text, or a short error string prefixed with ``↩ btw``.
    """
    from novacode_cli.agent_stream import run_agent_stream
    from novacode_cli.ui_events import AssistantMessage, Done, Error

    question = question.strip()
    if not question:
        return "Usage: /btw <question>"

    try:
        agent = get_btw_agent()
    except Exception as ex:  # noqa: BLE001 — surface as a message, never crash the turn
        return f"↩ btw: could not start web-search agent — {ex}"

    class _BtwSession:
        """Minimal session shim — only the fields the agent loop reads."""

        def __init__(self) -> None:
            self.thread_id = f"btw-{uuid.uuid4().hex[:12]}"
            self.active_goal: str | None = None
            self.plan_mode_enabled: bool = False
            self.auto_approve: bool = True

    answer_parts: list[str] = []
    try:
        async for e in run_agent_stream(
            question,
            agent,
            "btw-agent",
            _BtwSession(),
            backend=None,
            seen_message_ids=set(),
        ):
            if on_event is not None:
                on_event(e)
            if isinstance(e, AssistantMessage):
                answer_parts.append(e.text)
            elif isinstance(e, Error):
                # Surface a recognised provider failure (usage cap, etc.) as the
                # answer rather than silently returning "(no response)".
                return e.message if e.is_provider_notice else f"↩ btw failed: {e.message}"
            elif isinstance(e, Done):
                break
    except Exception as ex:  # noqa: BLE001
        return f"↩ btw failed: {ex}"

    return "\n\n".join(answer_parts).strip() or "(no response)"
