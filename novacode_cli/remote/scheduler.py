"""Cron / heartbeat scheduler — Loop-Engineering Enhancement 3 (event-driven).

The remote bridges are *reactive* — they only run when a human sends a message.
This module adds the *proactive* half: a :class:`CronScheduler` that fires tasks
on a schedule (a "heartbeat"), e.g. ``0 9 * * *`` → "every morning, review the
project and summarise what needs doing".

A fired job becomes a :class:`~novacode_cli.remote.bridge.RemoteMessage` with
``platform=CRON`` placed on the *same* ``asyncio.Queue`` the Discord/Telegram
bridges feed, so the existing processor runs it exactly like any other remote
prompt. There is no external chat to answer, so ``reply_fn`` is a no-op and the
run's output surfaces through the normal UI rendering.

Cron expressions use the standard five fields ``minute hour day-of-month month
day-of-week`` with ``*``, ``*/step``, ``a-b`` ranges and ``a,b,c`` lists. Day of
week is ``0=Sunday .. 6=Saturday`` (``7`` also accepted for Sunday). The parser
is intentionally small (no third-party ``croniter`` dependency).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from novacode_cli.events import cap_event_log, nova_event_log
from novacode_cli.hermes import config

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

    from novacode_cli.remote.bridge import RemoteMessage

logger = logging.getLogger("nova.remote.scheduler")

#: How often the tick loop wakes to test schedules. Cron granularity is one
#: minute, so a sub-minute interval guarantees no minute is missed.
_TICK_SECONDS = 20

_CRON_FIELDS = 5

#: Built-in learning-consolidation heartbeat (inspired by Prime Agent's
#: ``rlm-heartbeat``). Fires daily at 02:00 local time. The task is a
#: consolidation prompt that reviews the session's work and persists what was
#: learned into memory. Users can disable it with ``/cron remove <id>``.
_BUILTIN_CONSOLIDATION_CRON = "0 2 * * *"
_BUILTIN_CONSOLIDATION_ID = "nova-consolidation"
_BUILTIN_CONSOLIDATION_TASK = (
    "Learning consolidation heartbeat. Review the recent session(s): "
    "consolidate what you learned into long-term memory (user model, lessons, "
    "habits), note any skill or prompt refinements worth making, and record "
    "them. Be concise; do not invent work that was not done."
)


# ---------------------------------------------------------------------------
# Cron expression parsing (pure, unit-tested)
# ---------------------------------------------------------------------------


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the explicit set of values it matches."""
    values: set[int] = set()
    for part in field.split(","):
        token = part.strip()
        step = 1
        if "/" in token:
            token, step_str = token.split("/", 1)
            step = int(step_str)
            if step <= 0:
                msg = f"step must be positive: {part!r}"
                raise ValueError(msg)
        if token in ("*", ""):
            start, end = lo, hi
        elif "-" in token:
            start_str, end_str = token.split("-", 1)
            start, end = int(start_str), int(end_str)
        else:
            start = end = int(token)
        if start < lo or end > hi or start > end:
            msg = f"field out of range [{lo},{hi}]: {part!r}"
            raise ValueError(msg)
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into per-field match sets.

    Raises ``ValueError`` on a malformed expression.
    """
    fields = expr.split()
    if len(fields) != _CRON_FIELDS:
        msg = f"cron expression must have {_CRON_FIELDS} fields, got {len(fields)}: {expr!r}"
        raise ValueError(msg)
    minute = _parse_field(fields[0], 0, 59)
    hour = _parse_field(fields[1], 0, 23)
    dom = _parse_field(fields[2], 1, 31)
    month = _parse_field(fields[3], 1, 12)
    # Accept 7 as an alias for Sunday(0) before range-checking 0..6.
    dow = _parse_field(fields[4].replace("7", "0"), 0, 6)
    return minute, hour, dom, month, dow


def cron_matches(expr: str, when: datetime) -> bool:
    """Return whether ``when`` (minute resolution) satisfies the cron ``expr``."""
    minute, hour, dom, month, dow = parse_cron(expr)
    return (
        when.minute in minute
        and when.hour in hour
        and when.day in dom
        and when.month in month
        and (when.isoweekday() % 7) in dow
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class CronScheduler:
    """Fires scheduled tasks onto the shared remote-message queue.

    Args:
        queue: The shared ``asyncio.Queue`` the remote processor consumes.
        store: Durable store for persisting job definitions across restarts.
            ``None`` keeps jobs in memory only.
    """

    def __init__(self, queue: asyncio.Queue[RemoteMessage], store: BaseStore | None = None) -> None:
        """Hold the queue + store and initialise empty in-memory job state."""
        self._queue = queue
        self._store = store
        self._jobs: dict[str, dict[str, Any]] = {}
        self._last_fired: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Load persisted jobs and begin the tick loop (idempotent)."""
        async with self._start_lock:
            if self._task is not None and not self._task.done():
                return
            await self._load_jobs()
            await self._ensure_builtin_jobs()
            self._task = asyncio.create_task(self._run(), name="cron-scheduler")

    async def stop(self) -> None:
        """Cancel the tick loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        """Whether the tick loop is active."""
        return self._task is not None and not self._task.done()

    # -- Job management -----------------------------------------------------

    async def add_job(self, cron_expr: str, task: str, *, job_id: str | None = None) -> str:
        """Validate + register a job (persisted); returns its id.

        Raises ``ValueError`` if ``cron_expr`` is malformed.
        """
        parse_cron(cron_expr)  # validate eagerly so the caller sees the error
        jid = job_id or uuid.uuid4().hex[:8]
        record = {
            "job_id": jid,
            "cron_expr": cron_expr,
            "task": task,
            "created_at": time.time(),
        }
        await self._persist(jid, record)
        self._jobs[jid] = record
        return jid

    async def remove_job(self, job_id: str) -> bool:
        """Remove a job by id; returns whether it existed."""
        existed = job_id in self._jobs
        if existed and self._store is not None:
            await self._store.adelete(config.CRON_SCHEDULES_NS, job_id)
        self._jobs.pop(job_id, None)
        self._last_fired.pop(job_id, None)
        return existed

    def list_jobs(self) -> list[dict[str, Any]]:
        """Return the current job definitions (newest first)."""
        return sorted(self._jobs.values(), key=lambda j: j.get("created_at", 0.0), reverse=True)

    async def fire_now(self, task: str) -> None:
        """Enqueue a one-off task immediately (no schedule)."""
        await self._enqueue(task, job_id="manual")

    # -- Internals ----------------------------------------------------------

    async def _run(self) -> None:
        """Tick loop: every ``_TICK_SECONDS`` fire any jobs due this minute."""
        while True:
            try:
                await self._tick(datetime.now())  # noqa: DTZ005 — local wall clock
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cron tick failed")
            await asyncio.sleep(_TICK_SECONDS)

    async def _tick(self, now: datetime) -> None:
        """Fire every job whose schedule matches ``now`` (once per minute)."""
        stamp = now.strftime("%Y-%m-%d %H:%M")
        for jid, job in list(self._jobs.items()):
            if self._last_fired.get(jid) == stamp:
                continue  # already fired this minute
            try:
                matched = cron_matches(job["cron_expr"], now)
            except ValueError:
                logger.warning("Skipping job %s with bad cron %r", jid, job.get("cron_expr"))
                continue
            if matched:
                self._last_fired[jid] = stamp
                await self._enqueue(job["task"], job_id=jid)

    async def _enqueue(self, task: str, *, job_id: str) -> None:
        """Build a CRON RemoteMessage and put it on the shared queue."""
        from novacode_cli.remote.bridge import RemoteMessage, RemotePlatform

        msg = RemoteMessage(
            platform=RemotePlatform.CRON,
            chat_id=job_id,
            user_name="scheduler",
            text=task,
            reply_fn=_noop_reply,
        )
        await self._queue.put(msg)
        _emit_cron_event(f"⏰ Scheduled task fired ({job_id}): {task[:80]}")

    async def _persist(self, job_id: str, record: dict[str, Any]) -> None:
        if self._store is None:
            return
        await self._store.aput(config.CRON_SCHEDULES_NS, job_id, record)

    async def _load_jobs(self) -> None:
        if self._store is None:
            return
        try:
            items = []
            offset = 0
            while True:
                page = await self._store.asearch(config.CRON_SCHEDULES_NS, limit=100, offset=offset)
                items.extend(page)
                if len(page) < 100:
                    break
                offset += len(page)
        except Exception:
            logger.exception("Failed to load persisted cron jobs")
            raise
        for item in items or []:
            value = getattr(item, "value", None)
            if isinstance(value, dict) and value.get("cron_expr") and value.get("task"):
                self._jobs[value.get("job_id", getattr(item, "key", ""))] = dict(value)

    async def _ensure_builtin_jobs(self) -> None:
        """Register the built-in consolidation heartbeat if not already present.

        Idempotent: if the user has removed the built-in job (or it was never
        added), it is re-registered. If the user explicitly removed it, they can
        keep it removed by re-removing after this runs; there is no persistent
        "disabled" flag, matching the simple job model.
        """
        if _BUILTIN_CONSOLIDATION_ID in self._jobs:
            return
        try:
            await self.add_job(
                _BUILTIN_CONSOLIDATION_CRON,
                _BUILTIN_CONSOLIDATION_TASK,
                job_id=_BUILTIN_CONSOLIDATION_ID,
            )
            logger.info("Registered built-in consolidation heartbeat (%s)", _BUILTIN_CONSOLIDATION_ID)
        except Exception:
            logger.exception("Failed to register built-in consolidation heartbeat")


async def _noop_reply(_text: str) -> None:
    """Reply sink for sources with no chat to answer (cron / webhook)."""


def _emit_cron_event(message: str) -> None:
    """Surface a cron-fired notice through the TUI-safe event log."""
    try:
        nova_event_log.append(("nova_cron_fired", "⏰", "cyan", message))
        cap_event_log()
    except Exception:
        logger.exception("Failed to emit cron event")
