"""Session-only recurring work; timing never grants execution permission."""

from __future__ import annotations

import math
import re
import shlex
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

DEFAULT_TTL = 72 * 3600
MAX_TTL = 7 * 86400
MAX_LOOPS = 16
MAX_FILE_BYTES = 64 * 1024
CRON_FIELDS = 5
DEFAULT_PROMPT = (
    "Review this session and the current project for actionable maintenance. "
    "Check the status of work already requested. Avoid inventing work or making "
    "unrequested changes. Report relevant findings and stop when there is nothing to do."
)
current_loop: ContextVar[tuple[str, str] | None] = ContextVar("nova_current_loop", default=None)


def duration(text: str) -> float:
    """Parse a finite positive duration with an explicit unit."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text)
    if match is None:
        msg = "Use a duration such as 30s, 5m, 2h or 3d."
        raise ValueError(msg)
    value = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    if not math.isfinite(value) or value < 1:
        msg = "Durations must be finite and at least one second."
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class LoopSpec:
    """An empty prompt means instructions are loaded again on every iteration."""

    prompt: str = ""
    interval: float | None = None
    cron: str = ""
    ttl: float = DEFAULT_TTL


def cron_fields(expr: str) -> tuple[set[int], ...]:
    """Use Nova's field parser, handling cron's Sunday aliases independently."""
    from novacode_cli.remote.scheduler import _parse_field

    parts = expr.split()
    if len(parts) != CRON_FIELDS:
        msg = "Cron requires five fields inside quotes."
        raise ValueError(msg)
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    expanded = []
    for part, (low, high) in zip(parts, bounds, strict=True):
        tokens = part.split(",")
        if any(not token for token in tokens):
            msg = "Cron fields cannot contain empty list entries."
            raise ValueError(msg)
        normalized = []
        for token in tokens:
            base, separator, step = token.partition("/")
            normalized.append(f"{base}-{high}/{step}" if separator and base.isdigit() else token)
        expanded.append(_parse_field(",".join(normalized), low, high))
    values = tuple(expanded)
    return (*values[:4], {day % 7 for day in values[4]})


def next_cron(expr: str, after: datetime) -> datetime:
    """Find the next local minute, with standard day-of-month/day-of-week OR."""
    minutes, hours, days, months, weekdays = cron_fields(expr)
    parts = expr.split()
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for offset in range(366 * 8):
        day = start + timedelta(days=offset)
        dom, dow = day.day in days, day.isoweekday() % 7 in weekdays
        day_matches = (dom and dow) if "*" in (parts[2], parts[4]) else (dom or dow)
        if day.month not in months or not day_matches:
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                candidate = day.replace(hour=hour, minute=minute)
                if candidate >= start:
                    return candidate
    msg = "Cron has no occurrence within eight years."
    raise ValueError(msg)


def parse_loop(arguments: str) -> LoopSpec:
    """Parse an optional leading --ttl, schedule, and preserve inline prompt text."""
    rest = arguments.strip()
    ttl = float(DEFAULT_TTL)
    if rest.startswith("--ttl "):
        parts = rest.split(maxsplit=2)
        ttl = duration(parts[1])
        rest = parts[2] if len(parts) == 3 else ""  # noqa: PLR2004 - flag, value, remainder
    if not 1 <= ttl <= MAX_TTL:
        msg = "Loop TTL must be between one second and seven days."
        raise ValueError(msg)
    if not rest:
        return LoopSpec(ttl=ttl)
    if rest[0] in "\"'":
        stream = StringIO(rest)
        lexer = shlex.shlex(stream, posix=True)
        lexer.whitespace_split = True
        first = next(lexer)
        if len(first.split()) != CRON_FIELDS:
            msg = "Quote a five-field cron schedule; inline prompts need no quotes."
            raise ValueError(msg)
        next_cron(first, datetime.now())  # noqa: DTZ005 - local cron wall time
        # shlex's token stream consumed the closing quote and one separator.
        prompt = stream.read().strip()
        return LoopSpec(prompt=prompt, cron=first, ttl=ttl)
    first, _, prompt = rest.partition(" ")
    if re.fullmatch(r"\d+(?:\.\d+)?[smhd]", first):
        return LoopSpec(prompt=prompt.strip(), interval=duration(first), ttl=ttl)
    if first[0].isdigit():
        msg = "Invalid interval. Use 30s, 5m, 2h or a quoted cron schedule."
        raise ValueError(msg)
    return LoopSpec(prompt=rest, ttl=ttl)


def load_instructions(project: str, user_home: Path | None = None) -> str:
    """Read bounded UTF-8 instructions afresh; errors never trigger fallback work."""
    home = user_home if user_home is not None else Path.home()
    roots = [Path(project)] if project else []
    candidates = [root / folder / "loop.md" for root in roots for folder in (".nova", ".agent")]
    candidates += [home / folder / "loop.md" for folder in (".nova", ".agent")]
    for path in candidates:
        try:
            with path.open("rb") as handle:
                content = handle.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            continue
        if len(content) > MAX_FILE_BYTES:
            msg = f"Loop instructions exceed {MAX_FILE_BYTES} bytes: {path}"
            raise ValueError(msg)
        text = content.decode("utf-8-sig").strip()
        if not text:
            msg = f"Loop instructions are empty: {path}"
            raise ValueError(msg)
        return text
    return DEFAULT_PROMPT


@dataclass
class LoopJob:
    """One coalesced deadline, rather than a queue of missed ticks."""

    id: str
    spec: LoopSpec
    due: float
    expires: float
    running: bool = False
    iterations: int = 0
    rescheduled: bool = False


class LoopScheduler:
    """A session/thread owns these jobs. The UI owns the polling lifecycle."""

    def __init__(self, thread_id: str) -> None:
        """Start empty; definitions are never restored from saved conversations."""
        self.thread_id = thread_id
        self.jobs: dict[str, LoopJob] = {}
        self.closed = False

    def add(self, spec: LoopSpec, *, now: float | None = None) -> LoopJob:
        """Register a bounded recurrence without starting a turn."""
        if self.closed:
            msg = "This session's loop scheduler is closed."
            raise ValueError(msg)
        if len(self.jobs) >= MAX_LOOPS:
            msg = f"At most {MAX_LOOPS} loops can be active in one session."
            raise ValueError(msg)
        now = time.monotonic() if now is None else now
        if not 1 <= spec.ttl <= MAX_TTL:
            msg = "Loop TTL must be between one second and seven days."
            raise ValueError(msg)
        if spec.interval is not None and (not math.isfinite(spec.interval) or spec.interval < 1):
            msg = "Loop intervals must be finite and at least one second."
            raise ValueError(msg)
        job = LoopJob(uuid.uuid4().hex[:8], spec, now, now + spec.ttl)
        job.due = self._next(job, now) if spec.interval or spec.cron else now
        self.jobs[job.id] = job
        return job

    def _next(self, job: LoopJob, now: float) -> float:
        if job.spec.cron:
            wall = datetime.now()  # noqa: DTZ005 - local cron wall time
            return now + (next_cron(job.spec.cron, wall) - wall).total_seconds()
        return now + (job.spec.interval or 60)

    def expire(self, *, now: float | None = None) -> list[str]:
        """Delete expired jobs exactly once, including in-flight recurrences."""
        now = time.monotonic() if now is None else now
        expired = [key for key, job in self.jobs.items() if now >= job.expires]
        for key in expired:
            del self.jobs[key]
        return expired

    def take(self, *, idle: bool, now: float | None = None) -> LoopJob | None:
        """Claim at most one due job while idle; keep no catch-up queue."""
        now = time.monotonic() if now is None else now
        if self.closed or not idle or any(job.running for job in self.jobs.values()):
            return None
        due = [job for job in self.jobs.values() if job.due <= now < job.expires]
        if not due:
            return None
        job = min(due, key=lambda item: item.due)
        job.running, job.rescheduled = True, False
        job.due = self._next(job, now)
        return job

    def finish(self, job: LoopJob, *, retry: bool = False) -> None:
        """Release a claim and stop dynamic loops lacking an explicit wakeup."""
        job.running = False
        if retry:
            job.due = 0
        else:
            job.iterations += 1
            if not job.spec.interval and not job.spec.cron and not job.rescheduled:
                # A dynamic loop must explicitly choose its next wakeup.
                self.jobs.pop(job.id, None)

    def wakeup(self, job_id: str, delay: float = 60, *, stop: bool = False) -> dict:
        """Adjust an existing deadline without extending its expiration."""
        job = self.jobs.get(job_id)
        if job is None:
            return {"error": "Loop is no longer active."}
        if stop:
            self.jobs.pop(job_id, None)
            return {"loop_id": job_id, "stopped": True}
        if not math.isfinite(delay) or not 1 <= delay <= MAX_TTL:
            return {"error": "delay_seconds must be between 1 and 604800 seconds."}
        job.due = time.monotonic() + delay
        job.rescheduled = True
        return {"loop_id": job_id, "delay_seconds": delay}

    def stop(self, job_id: str = "all") -> int:
        """Remove exact identities, or all jobs when explicitly requested."""
        keys = list(self.jobs) if job_id == "all" else [job_id]
        return sum(self.jobs.pop(key, None) is not None for key in keys)

    def close(self) -> None:
        """Permanently fence this scheduler and discard its jobs."""
        self.stop()
        self.closed = True


def loop_command(state: Any, arguments: str) -> str:  # noqa: ANN401 - session adapters
    """Shared command semantics; never create a producer without an interactive host."""
    scheduler = getattr(state, "_loop_scheduler", None)
    thread = getattr(state, "thread_id", "")
    if scheduler is not None and scheduler.thread_id != thread:
        scheduler.close()
        scheduler = None
    args = arguments.strip()
    if args in {"list", "status"}:
        if scheduler is None or not scheduler.jobs:
            return "No active session loops."
        now = time.monotonic()
        return "\n".join(
            f"{job.id} · {job.spec.cron or str(job.spec.interval or 'dynamic')} · "
            f"{job.iterations} runs · expires in {max(0, int(job.expires - now))}s · "
            f"{job.spec.prompt or 'loop.md / built-in instructions'}"
            for job in scheduler.jobs.values()
        )
    if args == "stop" or args.startswith("stop "):
        target = args[4:].strip() or "all"
        count = scheduler.stop(target) if scheduler is not None else 0
        return f"Stopped {count} loop(s)."
    if getattr(state, "headless", False) or not getattr(state, "_loop_host", False):
        return "Session loops require an interactive Nova terminal. Use /cron for persisted jobs."
    spec = parse_loop(args)
    if scheduler is None or scheduler.closed:
        scheduler = LoopScheduler(thread)
        state._loop_scheduler = scheduler
    job = scheduler.add(spec)
    return f"Loop {job.id} scheduled for this session (TTL {int(spec.ttl)}s). /loop stop {job.id}"
