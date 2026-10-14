"""Bounded waits for shell jobs and remote background subagents."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from deepagents.middleware.async_subagents import AsyncSubAgent
    from langgraph_sdk.client import LangGraphClient

    from novacode_cli.shell.jobs import BackgroundJob


MAX_WAIT_SECONDS = 60
POLL_SECONDS = 0.5
MAX_OUTPUT_CHARS = 6000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Waiter:
    """One tool call currently waiting for a specific target."""

    id: int
    target: str
    kind: str
    started_at: float


_waiters: dict[int, Waiter] = {}
_waiters_lock = threading.Lock()
_waiter_ids = itertools.count(1)
_observers: list[Callable[[], None]] = []


def active_waiters() -> list[Waiter]:
    """Return a snapshot suitable for the TUI indicator."""
    with _waiters_lock:
        return list(_waiters.values())


def add_waiter_observer(callback: Callable[[], None]) -> None:
    """Notify a UI when the active waiter set changes."""
    with _waiters_lock:
        if callback not in _observers:
            _observers.append(callback)


def remove_waiter_observer(callback: Callable[[], None]) -> None:
    """Release a UI's observer when it closes."""
    with _waiters_lock:
        if callback in _observers:
            _observers.remove(callback)


def _notify_waiters() -> None:
    with _waiters_lock:
        observers = list(_observers)
    for callback in observers:
        try:
            callback()
        except Exception:  # noqa: BLE001 - a closed UI must not break the wait
            logger.debug("waiter observer failed", exc_info=True)


@contextmanager
def _tracking(target: str, kind: str) -> Iterator[None]:
    with _waiters_lock:
        waiter_id = next(_waiter_ids)
        _waiters[waiter_id] = Waiter(waiter_id, target, kind, time.monotonic())
    _notify_waiters()
    try:
        yield
    finally:
        with _waiters_lock:
            _waiters.pop(waiter_id, None)
        _notify_waiters()


def _bounded_timeout(seconds: int) -> int:
    return max(1, min(MAX_WAIT_SECONDS, seconds))


def _shell_result(job: BackgroundJob) -> str:
    from novacode_cli.shell.jobs import fmt_runtime

    parts = [f"{job.task_id}: {job.status}", f"runtime {fmt_runtime(job.runtime())}"]
    if job.exit_code is not None:
        parts.append(f"exit {job.exit_code}")
    output = job.output[-MAX_OUTPUT_CHARS:]
    if output:
        parts.append(f"Recent output:\n{output}")
    return " | ".join(parts)


async def _wait_shell(task_id: str, seconds: int) -> str:
    from novacode_cli.shell.jobs import get_registry

    registry = get_registry()
    job = registry.resolve(task_id)
    if job is None:
        return f"No background shell task '{task_id}'. Use list_background_tasks to find it."
    if job.status != "running":
        return _shell_result(job)
    loop = asyncio.get_running_loop()
    finished = loop.create_future()

    def resolve() -> None:
        if not finished.done():
            finished.set_result(None)

    def on_job(event: str, changed: BackgroundJob | None) -> None:
        if changed is job and event in {"completed", "failed", "terminated"}:
            loop.call_soon_threadsafe(resolve)

    with _tracking(job.task_id, "shell command"):
        registry.add_observer(on_job)
        try:
            if job.status == "running":
                with suppress(TimeoutError):
                    await asyncio.wait_for(finished, seconds)
        finally:
            registry.remove_observer(on_job)
    return _shell_result(job)


def _remote_client(spec: AsyncSubAgent) -> LangGraphClient:
    from langgraph_sdk import get_client

    headers = dict(spec.get("headers") or {})
    headers.setdefault("x-auth-scheme", "langsmith")
    return get_client(url=spec.get("url"), headers=headers)


async def _wait_remote(task: dict, seconds: int) -> dict:
    from novacode_cli.agents.default_subagents.async_subagents import retrieve_async_subagents

    specs = {spec["name"]: spec for spec in retrieve_async_subagents()}
    spec = specs.get(str(task.get("agent_name", "")))
    if spec is None:
        return {"status": "unavailable", "error": "The background subagent is unavailable."}
    client = _remote_client(spec)
    deadline = time.monotonic() + seconds
    with _tracking(task["task_id"], f"subagent {task['agent_name']}"):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "running", "thread_id": task["thread_id"], "timed_out": True}
            try:
                run = await asyncio.wait_for(
                    client.runs.get(thread_id=task["thread_id"], run_id=task["run_id"]),
                    timeout=remaining,
                )
            except TimeoutError:
                return {"status": "running", "thread_id": task["thread_id"], "timed_out": True}
            except Exception as exc:  # noqa: BLE001 - SDK errors are untyped
                return {"status": "unavailable", "error": f"Could not check subagent: {exc}"}
            status = run.get("status", "unknown")
            if status not in {"pending", "running"}:
                result = {"status": status, "thread_id": task["thread_id"]}
                if status == "success":
                    try:
                        thread = await asyncio.wait_for(
                            client.threads.get(thread_id=task["thread_id"]),
                            timeout=max(0.01, deadline - time.monotonic()),
                        )
                        messages = (thread.get("values") or {}).get("messages") or []
                        last = messages[-1] if messages else None
                        result["result"] = (
                            last.get("content", "") if isinstance(last, dict) else str(last or "")
                        )
                    except Exception as exc:  # noqa: BLE001 - preserve completed status
                        result["result"] = f"Completed, but result could not be fetched: {exc}"
                elif status == "error":
                    result["error"] = str(run.get("error") or "The subagent failed.")
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": status, "thread_id": task["thread_id"], "timed_out": True}
            await asyncio.sleep(min(POLL_SECONDS, remaining))


def _remote_result(task: dict, result: dict, runtime: ToolRuntime) -> Command:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = {**task, "status": result["status"], "last_checked_at": now}
    if result["status"] != task.get("status"):
        updated["last_updated_at"] = now
    return Command(
        update={
            "messages": [ToolMessage(json.dumps(result), tool_call_id=runtime.tool_call_id)],
            "async_tasks": {task["task_id"]: updated},
        }
    )


@tool
async def wait_for_task(
    task_id: str, runtime: ToolRuntime, timeout_seconds: int = 30
) -> str | Command:
    """Wait up to 60 seconds for a shell job or background subagent to finish.

    Returns its current status and result/output, even when the wait times out.
    Call again to keep waiting. Foreground commands already wait automatically.

    Args:
        task_id: Shell task ID (task_42 or 42) or exact background subagent ID.
        runtime: Injected tool runtime containing tracked background subagents.
        timeout_seconds: Maximum wait for this call, from 1 to 60 seconds.
    """
    seconds = _bounded_timeout(timeout_seconds)
    from novacode_cli.shell.jobs import get_registry

    if get_registry().resolve(task_id) is not None or task_id.strip().lower().startswith("task_"):
        return await _wait_shell(task_id, seconds)
    task = (runtime.state.get("async_tasks") or {}).get(task_id.strip())
    if task is None:
        return f"No tracked task '{task_id}'. Use list_async_tasks or list_background_tasks."
    result = await _wait_remote(task, seconds)
    if result["status"] == "unavailable":
        return result["error"]
    return _remote_result(task, result, runtime)


@tool
def list_waiters() -> str:
    """List active waits and the shell command or subagent each is waiting for."""
    waiters = active_waiters()
    if not waiters:
        return "No active waiters."
    now = time.monotonic()
    lines = [f"{len(waiters)} active waiter(s):"]
    lines.extend(
        f"  waiter_{item.id}: {item.kind} {item.target} ({int(now - item.started_at)}s)"
        for item in waiters
    )
    return "\n".join(lines)


__all__ = [
    "active_waiters",
    "add_waiter_observer",
    "list_waiters",
    "remove_waiter_observer",
    "wait_for_task",
]
