"""The model may reschedule existing session loops, never create recurring work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.tools import tool

from novacode_cli.loops import current_loop

if TYPE_CHECKING:
    from novacode_cli.states.Session import SessionState


def build_loop_tools(session_state: SessionState) -> list:
    """Bind wakeup control to the session that owns the compiled agent."""

    @tool
    async def schedule_wakeup(
        delay_seconds: float = 60, *, stop: bool = False, loop_id: str = ""
    ) -> dict:
        """Set the next wakeup of an existing /loop, or stop it when its goal is met.

        Use stop=true for a user's natural-language cancellation request. During
        a loop iteration the current loop is selected automatically; otherwise
        provide loop_id when multiple loops exist. This does not create loops,
        extend their TTL, approve tools, or change execution permissions.
        """
        scheduler = getattr(session_state, "_loop_scheduler", None)
        if scheduler is None or scheduler.thread_id != session_state.thread_id or scheduler.closed:
            return {"error": "No active loop scheduler for this conversation."}
        active = current_loop.get()
        chosen = loop_id or (active[1] if active and active[0] == scheduler.thread_id else "")
        if not chosen and len(scheduler.jobs) == 1:
            chosen = next(iter(scheduler.jobs))
        if not chosen:
            return {"error": "Choose a loop_id.", "loop_ids": list(scheduler.jobs)}
        return scheduler.wakeup(chosen, delay_seconds, stop=stop)

    return [schedule_wakeup]
