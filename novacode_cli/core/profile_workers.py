"""Background agent threads scoped to the turn that requested them."""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain.tools import ToolRuntime, tool
from pydantic import Field
from typing_extensions import Annotated

from novacode_cli import ui_events as ev
from novacode_cli.agents.profiles import AgentProfile, active_profile, profile_readiness
from novacode_cli.config.plan_mode import active_plan_session
from novacode_cli.core.subagent_tasks import PROFILE_PHASE

if TYPE_CHECKING:
    from collections.abc import Callable

current_workers: ContextVar[ProfileWorkers | None] = ContextVar(
    "nova_profile_workers", default=None
)


@tool
async def summon_agent(
    runtime: ToolRuntime,
    agent: AgentProfile | None = None,
    task: Annotated[str, Field(max_length=16000)] = "",
    worker_id: str = "",
) -> dict:
    """Start an agent in a separate background conversation, or collect its result.

    Set agent (nova, serena, nora, or ralph) and task to start. Give a self-contained
    brief with the objective, relevant files or sources, constraints, and the
    deliverable or acceptance check. Use available results as evidence and verify
    them before acting. Continue independent work while the agent runs. Call again
    with only worker_id to await and read the result before ending the turn.
    """
    workers = current_workers.get()
    session = active_plan_session.get()
    if (
        workers is None
        or active_profile() == "ralph"
        or getattr(session, "plan_mode_enabled", False)
    ):
        return {"error": "Agent workers are unavailable in this mode."}
    if worker_id:
        if agent or task:
            return {"error": "Use only worker_id when collecting a result."}
        worker = workers.tasks.get(worker_id)
        if worker is None:
            return {"error": "Unknown worker for this turn."}
        return await asyncio.shield(worker)
    if agent is None or not task.strip():
        return {"error": "Choose an agent and provide a task."}
    if reason := profile_readiness(agent):
        return {"error": reason}
    if workers.budget[0] <= 0 or workers.depth >= 2:
        return {"error": "The delegation limit for this turn has been reached."}
    workers.budget[0] -= 1
    worker_id = uuid4().hex
    row = ev.SubagentTask(
        task_id=worker_id,
        phase_kind="code",
        phase_id=PROFILE_PHASE,
        subagent_type=agent.title(),
        description=task,
        started_at=time.time(),
    )
    runtime.stream_writer({"type": "nova_profile_worker", "event": row})
    workers.tasks[worker_id] = asyncio.create_task(
        workers.run(agent, task, row, runtime.stream_writer), name=f"nova-{agent}-{worker_id}"
    )
    return {"worker_id": worker_id, "agent": agent.title(), "status": "running"}


class ProfileWorkers:
    def __init__(self, agent, backend, assistant_id, session, parent: ProfileWorkers | None):
        self.agent = agent
        self.backend = backend
        self.assistant_id = assistant_id
        self.session = session
        self.tasks: dict[str, asyncio.Task[dict]] = {}
        self.depth = parent.depth + 1 if parent is not None else 0
        self.budget = parent.budget if parent is not None else [4]

    async def close(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def run(
        self,
        profile: AgentProfile,
        prompt: str,
        row: ev.SubagentTask,
        writer: Callable,
    ) -> dict:
        from novacode_cli.core.agent_loop import iterate_agent_events
        from novacode_cli.states.Session import SessionState

        def emit(event) -> None:
            writer({"type": "nova_profile_worker", "event": event})

        status, error = "failed", ""
        replies: list[str] = []
        child = SessionState(auto_approve=self.session.auto_approve, no_splash=True)
        child.agent_profile = profile
        child.workspace_root = getattr(self.session, "workspace_root", None)
        child.handoff_enabled = getattr(self.session, "handoff_enabled", False)
        child.prime_enabled = getattr(self.session, "prime_enabled", False)
        child._sandbox_type = getattr(self.session, "_sandbox_type", None)
        tracker = getattr(self.session, "token_tracker", None)
        window = getattr(tracker, "context_window_size", 0)
        usage = 0

        async def execute(text, *_args, **_kwargs) -> bool:
            nonlocal usage, error
            # Each call inherits the owner's current approval mode.
            child.auto_approve = self.session.auto_approve
            stream = iterate_agent_events(
                text,
                self.agent,
                self.assistant_id,
                child,
                backend=self.backend,
                skip_file_mentions=True,
            )
            async with aclosing(stream):
                async for event in stream:
                    if isinstance(
                        event, (ev.InterruptRequest, ev.SubagentTask, ev.SubagentPreview)
                    ):
                        emit(event)
                    elif isinstance(event, ev.ContextUsage):
                        usage = event.input_tokens
                        emit(replace(row, context_used=usage, context_window=window))
                    elif isinstance(event, ev.AssistantMessage) and not event.is_subagent:
                        replies.append(event.text[-16000:])
                        if len(replies) > 8:
                            replies.pop(0)
                        emit(ev.SubagentPreview(row.task_id, "text", event.text[-2000:]))
                    elif isinstance(event, (ev.Error, ev.ContextOverflow, ev.Cancelled)):
                        error = getattr(event, "message", "Cancelled")
                        return False
                    elif isinstance(event, ev.Done):
                        return True
            return False

        try:
            if profile == "ralph":
                from novacode_cli.commands import ralph_events as rev
                from novacode_cli.commands.ralph_handler import handle_ralph_command

                finish = None

                def progress(event) -> None:
                    nonlocal finish
                    if isinstance(event, rev.RalphFinished):
                        finish = event

                await handle_ralph_command(
                    self.agent,
                    child,
                    self.assistant_id,
                    tracker,
                    [prompt],
                    execute_fn=execute,
                    emit=lambda message: emit(ev.SubagentPreview(row.task_id, "text", message)),
                    on_event=progress,
                )
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                if finish is not None:
                    status = "done" if finish.reason == "complete" else "failed"
                    error = finish.detail if status == "failed" else ""
                else:
                    error = "Ralph could not start. See the worker preview."
            elif await execute(prompt):
                status = "done"
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001 - report worker failures to the owning turn.
            error = str(exc)[:300]
        finally:
            emit(
                replace(
                    row,
                    status=status,
                    error=error[:300],
                    context_used=usage,
                    context_window=window,
                    duration_ms=int((time.time() - row.started_at) * 1000),
                )
            )
        result = {
            "agent": profile.title(),
            "status": status,
            "result": "\n\n".join(replies)[-16000:],
        }
        if error:
            result["error"] = error[:300]
        return result
