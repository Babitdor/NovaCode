"""Bind in-memory session loops to normal serialized TUI turns."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from rich.text import Text

from novacode_cli.loops import LoopJob, LoopSpec, current_loop, load_instructions, loop_command

# TUI and focused test hosts expose the same runtime attributes.
# ruff: noqa: ANN401


def loop_idle(app: Any, state: Any) -> bool:
    """Never switch conversations or compete with user work/approval dialogs."""
    lock = getattr(state, "_remote_message_lock", None)
    remote = getattr(app, "_remote_turn_task", None)
    queue = getattr(state, "_remote_message_queue", None)
    return (
        app.session_state is state
        and getattr(getattr(app, "_active_pane", None), "kind", "root") == "root"
        and not getattr(app, "_turn_active", False)
        and not (lock is not None and lock.locked())
        and not (remote is not None and not remote.done())
        and not getattr(app, "_deferred_commands", ())
        and not getattr(app, "_deferred_prompts", ())
        and not getattr(state, "approved_plan_content", None)
        and not getattr(state, "plan_mode_enabled", False)
        and not (queue is not None and not queue.empty())
        and len(getattr(app, "screen_stack", ())) <= 1
    )


async def run_loop_command(app: Any, text: str) -> None:
    """Register the host lazily; status reports are explicit transcript output."""
    state = app.session_state
    if getattr(getattr(app, "_active_pane", None), "kind", "root") != "root":
        app._log(Text("Session loops currently require a local Nova tab.", style="yellow"))
        return
    args = text.partition(" ")[2].strip()
    state._loop_host = True
    states = getattr(app, "_loop_states", None)
    if states is None:
        states = app._loop_states = []
        app._loop_poll_timer = app.set_interval(1, lambda: poll_loops(app))
    if not any(existing is state for existing in states):
        states.append(state)
    try:
        message = loop_command(state, args)
    except (ValueError, OSError) as exc:
        app._log(Text(f"/loop: {exc}", style="yellow"))
        return
    app._log(Text(message), transcript=args in {"list", "status"})
    active = getattr(app, "_active_loop_job", None)
    if active is not None and active[0] is state:
        scheduler = getattr(state, "_loop_scheduler", None)
        if scheduler is None or active[1].id not in scheduler.jobs:
            app.action_cancel_turn()


def poll_loops(app: Any) -> None:
    """One bounded tick, no prompt backlog; inactive tabs keep a single deadline."""
    for state in list(getattr(app, "_loop_states", ())):
        scheduler = getattr(state, "_loop_scheduler", None)
        if scheduler is None:
            continue
        if scheduler.thread_id != state.thread_id:
            scheduler.close()
        if scheduler.closed:
            app._loop_states.remove(state)
            continue
        for job_id in scheduler.expire():
            state.add_notification(
                "info", "Loop expired", f"Loop {job_id} reached its TTL.", source="loop"
            )
            active = getattr(app, "_active_loop_job", None)
            if active is not None and active[0] is state and active[1].id == job_id:
                if app.session_state is state:
                    app.action_cancel_turn()
                else:
                    worker = getattr(app, "_loop_workers", {}).get((scheduler.thread_id, job_id))
                    if worker is not None:
                        worker.cancel()
        job = scheduler.take(idle=loop_idle(app, state))
        if job is not None:
            workers = getattr(app, "_loop_workers", None)
            if workers is None:
                workers = app._loop_workers = {}
            workers[(scheduler.thread_id, job.id)] = app._run_loop_job(state, job)


async def _iteration_prompt(state: Any, spec: LoopSpec, assistant_id: str) -> str:
    project = getattr(state, "workspace_root", None)
    if not project:
        from novacode_cli.config.config import settings

        project = settings.get_workspace_root()
    prompt = spec.prompt or await asyncio.to_thread(load_instructions, str(project or ""))
    if not prompt.startswith("/"):
        return prompt
    from novacode_cli.commands.skill_invoke import _try_skill_invocation

    name, _, arguments = prompt[1:].partition(" ")
    if arguments.strip().startswith("--run"):
        msg = "Loop skills use the agent's tool approvals; direct --run is unsupported."
        raise ValueError(msg)
    skill = await _try_skill_invocation(
        name.removeprefix("skill:"), arguments or None, state, assistant_id
    )
    if skill is None:
        msg = f"Unknown loop skill: /{name}"
        raise ValueError(msg)
    return skill.prompt


async def run_loop_iteration(app: Any, state: Any, job: LoopJob) -> None:
    """Resolve instructions off-thread, recheck ownership, then use the normal turn."""
    scheduler = state._loop_scheduler
    app._active_loop_job = (state, job)
    token = current_loop.set((scheduler.thread_id, job.id))
    retry = False
    try:
        prompt = await _iteration_prompt(state, job.spec, app.assistant_id)
        if (
            scheduler.closed
            or scheduler.thread_id != state.thread_id
            or job.id not in scheduler.jobs
            or time.monotonic() >= job.expires
        ):
            return
        if not loop_idle(app, state):
            retry = True
            return
        instruction = (
            f"[Session loop {job.id}]\n{prompt}\n\n"
            "This iteration inherits the session's existing tool permissions and approvals. "
            "Use schedule_wakeup(stop=true) when the task is complete or cancelled. "
            "For a self-paced loop, call schedule_wakeup(delay_seconds=...) to continue; "
            "without that call it stops after this iteration."
        )
        app._log(Text(f"↻ Running loop {job.id}", style="dim"))
        await app._stream_prompt(instruction)
        if getattr(app, "_stream_turn_failed", False):
            scheduler.stop(job.id)
    except asyncio.CancelledError:
        scheduler.stop(job.id)
        raise
    except Exception as exc:  # noqa: BLE001 - an invalid loop must not kill the host timer
        scheduler.stop(job.id)
        app._log(Text(f"Loop {job.id} stopped: {exc}", style="yellow"))
    finally:
        current_loop.reset(token)
        scheduler.finish(job, retry=retry)
        getattr(app, "_loop_workers", {}).pop((scheduler.thread_id, job.id), None)
        if getattr(app, "_active_loop_job", None) == (state, job):
            app._active_loop_job = None


def close_loops(app: Any) -> None:
    """Session shutdown never persists or restarts loops."""
    timer = getattr(app, "_loop_poll_timer", None)
    if timer is not None:
        timer.stop()
    for state in getattr(app, "_loop_states", ()):
        scheduler = getattr(state, "_loop_scheduler", None)
        if scheduler is not None:
            scheduler.close()
        state._loop_host = False
