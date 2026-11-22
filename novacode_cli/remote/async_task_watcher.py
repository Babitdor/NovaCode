"""Watch remote async-subagent runs and notify when they finish.

deepagents' async subagents are fire-and-forget: ``start_async_task`` returns a
``task_id`` and the main agent is told to stop, nothing pushes completion back.
This poller closes that gap. Given the tasks recorded in the agent state
(``async_tasks``), it polls each running task's remote status via the LangGraph
SDK and fires a notification (through the injected callback) when it reaches a
terminal state. Best-effort throughout: a poll failure never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any, Callable

from novacode_cli.remote.async_task_clients import AsyncTaskClients

if TYPE_CHECKING:
    from langgraph_sdk.client import LangGraphClient

logger = logging.getLogger("nova.async_task_watcher")

# LangGraph Run.status values that mean "no longer running".
_TERMINAL = frozenset({"success", "error", "timeout", "interrupted", "cancelled"})
_POLL_INTERVAL = 6.0
_MAX_WATCH_SECONDS = 300.0  # warn once after five minutes; keep watching
#: How often a task may fail its setup (spec discovery, client construction)
#: before it stops being re-watched on every turn end and is declared
#: unmonitorable once, with a notice.
_MAX_SETUP_TRIES = 3
_RUN_TIMEOUT = 10.0
_MAX_CONCURRENT_POLLS = 8
_MAX_PENDING_POLLS = 256
_MAX_WATCHED_IDS = 512


class AsyncTaskWatcher:
    """Polls launched async-subagent runs; notifies on completion. Idempotent."""

    def __init__(self, notify: Callable[[str, str, str], Any]) -> None:
        # notify(level, title, message) — surfaces a completed task to the user.
        self._notify = notify
        self._watched: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._pending: deque[tuple[str, dict]] = deque()
        self._pending_ids: set[str] = set()
        self._closed = False
        self._poll_slots = asyncio.Semaphore(_MAX_CONCURRENT_POLLS)
        self._specs: dict[str, dict] | None = None
        self._spec_lock = asyncio.Lock()
        self._setup_tries: dict[str, int] = {}
        self._clients = AsyncTaskClients()
        # Currently-running tasks, for the TUI indicator: task_id -> {agent_name, started}.
        self._running: dict[str, dict] = {}

    def _spec_for(self, agent_name: str) -> dict | None:
        """The async-subagent spec (url + headers) for a given agent name."""
        if self._specs is None:
            try:
                from novacode_cli.agents.default_subagents.async_subagents import (
                    retrieve_async_subagents,
                )

                self._specs = {s["name"]: dict(s) for s in retrieve_async_subagents()}
            except Exception:  # noqa: BLE001
                return None
        return self._specs.get(agent_name)

    async def invalidate_specs(self) -> None:
        """Forget cached agent specs so the next poll rediscovers them.

        The cache is populated once per watcher; without this a ``/model``
        change to the async role (or edited agent files) leaves polls using
        stale URLs/headers for the session's life.

        The clear happens under ``_spec_lock`` — the same lock a cold
        discovery holds in :meth:`_resolve_spec` — so an in-flight discovery
        finishes its write before we clear, and cannot write stale specs
        back after invalidation.
        """
        async with self._spec_lock:
            self._specs = None
            self._setup_tries.clear()

    def sync_from_state(self, async_tasks: dict[str, Any] | None) -> None:
        """Start watching any running task in *async_tasks* not already watched."""
        if self._closed:
            return
        skipped = 0
        for task_id, task in (async_tasks or {}).items():
            if not isinstance(task, dict) or task.get("status") != "running":
                continue
            task_id = str(task_id)
            if task_id in self._watched or task_id in self._pending_ids:
                continue
            if len(self._pending) >= _MAX_PENDING_POLLS:
                skipped += 1
                continue
            task = dict(task)
            # The state map key is the identity used for _watched/_running/
            # notifications; an embedded field that disagrees with it would
            # split the task across two identities.
            task["task_id"] = task_id
            self._pending.append((task_id, task))
            self._pending_ids.add(task_id)
            self._running[task_id] = {
                "agent_name": task.get("agent_name") or "async agent",
                "started": time.monotonic(),
            }
        if skipped:
            logger.warning("async task watcher queue is full; deferred %d task(s)", skipped)
        self._start_available()

    async def _resolve_spec(self, agent_name: str) -> dict | None:
        """Serialize cold discovery without blocking terminal input or repainting."""
        async with self._spec_lock:
            if self._specs is not None:
                return self._specs.get(agent_name)
            return await asyncio.to_thread(self._spec_for, agent_name)

    def _start_available(self) -> None:
        while not self._closed and self._pending and len(self._tasks) < _MAX_PENDING_POLLS:
            task_id, task = self._pending.popleft()
            self._pending_ids.discard(task_id)
            self._watched[task_id] = None
            self._watched.move_to_end(task_id)
            # Eviction must spare ids whose poll is still in flight: if state
            # still lists the task as running, a later sync would re-watch it
            # and emit a second completion (second auto-report turn).
            attempts = 0
            while len(self._watched) > _MAX_WATCHED_IDS and attempts < len(self._watched):
                oldest, _ = self._watched.popitem(last=False)
                attempts += 1
                if oldest in self._running:
                    self._watched[oldest] = None
                    self._watched.move_to_end(oldest)
            poll = asyncio.create_task(self._poll(task), name=f"async-task-watch-{task_id[:8]}")
            self._tasks.add(poll)
            poll.add_done_callback(self._poll_done)

    def _poll_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._start_available()

    async def aclose(self) -> None:
        """Cancel and await polls when the owning TUI shuts down."""
        self._closed = True
        self._pending.clear()
        self._pending_ids.clear()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.clear()
            self._running.clear()
            await self._clients.aclose()

    def running_tasks(self) -> list[dict]:
        """Snapshot of currently-running async tasks (for the TUI indicator)."""
        now = time.monotonic()
        return [
            {"task_id": tid, "agent_name": v["agent_name"], "runtime": now - v["started"]}
            for tid, v in self._running.items()
        ]

    async def _poll(self, task: dict) -> None:
        task_id = str(task.get("task_id") or task.get("thread_id") or "?")
        agent_name = task.get("agent_name") or "async agent"
        completed = False
        permanent = False
        try:
            thread_id, run_id = task.get("thread_id"), task.get("run_id")
            if (
                not isinstance(thread_id, str)
                or not isinstance(run_id, str)
                or not thread_id
                or not run_id
            ):
                # A record without ids cannot ever be polled; retrying on
                # every turn end would loop forever with no notice.
                permanent = True
                return
            tries = self._setup_tries.get(task_id, 0) + 1
            spec = await self._resolve_spec(agent_name)
            if spec is None:
                self._setup_tries[task_id] = tries
                if tries >= _MAX_SETUP_TRIES:
                    permanent = True
                return
            try:

                def create_client() -> LangGraphClient:
                    from langgraph_sdk import get_client

                    return get_client(url=spec.get("url"), headers=spec.get("headers") or {})

                client = await self._clients.get(spec, create_client)
            except Exception:  # noqa: BLE001
                if tries >= _MAX_SETUP_TRIES:
                    permanent = True
                return
            self._setup_tries.pop(task_id, None)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + _MAX_WATCH_SECONDS
            warned_slow = False
            failures = 0
            while True:
                try:
                    # Limit concurrent HTTP calls, not entire task lifetimes:
                    # eight slow agents must not hide every later completion.
                    async with self._poll_slots:
                        async with asyncio.timeout(_RUN_TIMEOUT):
                            run = await client.runs.get(thread_id, run_id)
                    status = (run or {}).get("status", "running")
                    if failures >= 3:
                        self._notice(
                            agent_name, task_id, "Connection restored", "Monitoring has resumed."
                        )
                    failures = 0
                except Exception:  # noqa: BLE001 - transient; keep monitoring
                    failures += 1
                    if failures == 3:
                        self._notice(
                            agent_name,
                            task_id,
                            "Connection interrupted",
                            "Cannot reach the agent server. Nova is retrying status checks; "
                            "the task may still be running.",
                        )
                    status = "running"
                if status in _TERMINAL:
                    completed = True
                    self._emit(agent_name, task_id, status)
                    return
                if not warned_slow and loop.time() >= deadline:
                    warned_slow = True
                    self._notice(
                        agent_name,
                        task_id,
                        "Taking longer than expected",
                        "The task is still active. Provider retries or slow tools may be delaying it. "
                        "Nova will keep monitoring until it finishes or the session closes.",
                    )
                await asyncio.sleep(_POLL_INTERVAL)
        finally:
            # Drop from the running set on every exit path so the TUI indicator clears.
            self._running.pop(task_id, None)
            if permanent:
                # Unmonitorable after the setup budget (or structurally broken):
                # keep it in _watched so every turn-end sync does not re-enqueue
                # it forever, and say so once instead of failing silently.
                self._setup_tries.pop(task_id, None)
                self._notice(
                    agent_name,
                    task_id,
                    "No longer monitorable",
                    "Its run could not be resolved (missing ids or agent spec). "
                    "Use check_async_task if you need its result.",
                )
            elif not completed:
                # Failed setup/cancelled monitoring is not a completed remote run.
                self._watched.pop(task_id, None)

    def _notice(self, agent_name: str, task_id: str, title: str, detail: str) -> None:
        """Nonterminal progress must not trigger automatic result fetching."""
        try:
            self._notify(
                "warning", f"Async agent: {title}", f"{agent_name} ({task_id[:8]}): {detail}"
            )
        except Exception:  # noqa: BLE001
            logger.debug("async task notification failed", exc_info=True)

    def _emit(self, agent_name: str, task_id: str, status: str) -> None:
        level = "success" if status == "success" else "warning"
        verb = "completed" if status == "success" else status
        title = f"Async agent {verb}"
        message = (
            f"{agent_name} ({task_id[:8]}) {verb}. "
            f"Ask me to check_async_task {task_id[:8]} for the result."
            # Full task_id is carried for consumers that auto-report the result
            # (the TUI parses it to drive a proactive turn); the human-readable
            # prefix above keeps the truncated form for display.
            f"\n[async_task_id={task_id}]"
        )
        try:
            self._notify(level, title, message)
        except Exception:  # noqa: BLE001 — a notification must never break the watcher
            pass
