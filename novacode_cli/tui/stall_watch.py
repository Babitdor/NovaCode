"""Record what the UI event loop was doing whenever it freezes.

The Textual UI and the agent share one asyncio loop, so ANY synchronous work on
it (a subprocess, a big file read, a sync store call in a middleware) freezes
the whole session, and afterwards there is no trace of what did it. This
watchdog pings the loop from a daemon thread; when a ping goes unanswered for
``threshold`` seconds it samples the loop thread's Python stack, and logs every
distinct sample with the stall's duration to ``~/.nova/logs/freeze.log``.

Cost when nothing is stuck: one ``call_soon_threadsafe`` every 0.25s.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(os.environ.get("NOVA_FREEZE_LOG") or Path.home() / ".nova" / "logs" / "freeze.log")
_MAX_LOG_BYTES = 2_000_000
_PING_EVERY = 0.25
_SAMPLE_EVERY = 1.0
_MAX_STALLS = 32
_MAX_SAMPLES = 4
_MAX_SAMPLE_CHARS = 16_384
#: A thread whose innermost frame is one of these is waiting, not working.
_IDLE_FRAMES = frozenset({"wait", "_wait_for_tstate_lock", "get", "select", "_worker", "accept", "recv", "read", "readline", "sleep", "run_forever"})


class StallWatch:
    """Watch one event loop (run :meth:`start` from the loop's own thread)."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold: float = 1.0,
        log_path: Path = LOG_PATH,
    ) -> None:
        self._loop = loop
        self._threshold = threshold
        self._log_path = log_path
        self._loop_thread = threading.get_ident()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stalls: deque[tuple[float, list[str]]] = deque(maxlen=_MAX_STALLS)

    def start(self) -> None:
        # Tests stall the loop on purpose; don't fill the user's log with them.
        if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("NOVA_FREEZE_LOG"):
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="nova-stall-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _sample(self) -> str:
        frames = sys._current_frames()
        frame = frames.get(self._loop_thread)
        if frame is None:
            return "(loop thread gone)"
        stack = "".join(traceback.format_stack(frame))  # whole stack: the caller matters
        # The loop may only be where it was when another thread took the GIL
        # (a C extension holds it until done): show what the others are doing.
        names = {t.ident: t.name for t in threading.enumerate()}
        busy = []
        for ident, other in frames.items():
            if ident in (self._loop_thread, threading.get_ident()):
                continue
            top = traceback.extract_stack(other)[-4:]
            if top and top[-1].name in _IDLE_FRAMES:
                continue
            where = " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(top))
            busy.append(f"  [{names.get(ident, ident)}] {where}\n")
        return stack + ("--- other threads ---\n" + "".join(busy) if busy else "")

    @staticmethod
    def _resources() -> tuple[float, str]:
        """This process's CPU seconds so far (all threads), and memory now."""
        times = os.times()
        cpu = times.user + times.system
        try:
            import psutil

            rss = psutil.Process().memory_info().rss / 2**20
            free = psutil.virtual_memory().available / 2**30
            return cpu, f"rss {rss:.0f} MB, system free {free:.1f} GB"
        except Exception:  # noqa: BLE001 - diagnostics only
            return cpu, ""

    def _run(self) -> None:
        while not self._stop.is_set():
            answered = threading.Event()
            started = time.monotonic()
            cpu_before, _ = self._resources()
            try:
                self._loop.call_soon_threadsafe(answered.set)
            except RuntimeError:  # loop closed: the app is gone
                return
            if answered.wait(self._threshold):
                self._stop.wait(_PING_EVERY)
                continue
            samples: list[str] = []
            memory = ""
            while not answered.wait(0 if not samples else _SAMPLE_EVERY):
                stack = self._sample()
                if not samples:
                    memory = self._resources()[1]
                if len(samples) < _MAX_SAMPLES and stack not in samples:
                    samples.append(stack[-_MAX_SAMPLE_CHARS:])
                if self._stop.is_set():
                    return
            seconds = time.monotonic() - started
            cpu = self._resources()[0] - cpu_before
            # CPU ~ wall time: Nova was busy (see the threads). CPU ~ 0: Nova
            # was not running at all -- paging, disk, antivirus, the OS.
            self._record(seconds, samples, f"cpu {cpu:.1f}s" + (f", {memory}" if memory else ""))

    def _record(self, seconds: float, samples: list[str], resources: str = "") -> None:
        samples = [sample[-_MAX_SAMPLE_CHARS:] for sample in samples[-_MAX_SAMPLES:]]
        self.stalls.append((seconds, samples))
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_path.exists() and self._log_path.stat().st_size > _MAX_LOG_BYTES:
                self._log_path.replace(self._log_path.with_suffix(".log.1"))
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} UI loop frozen "
                    f"{seconds:.1f}s ({len(samples)} distinct stack(s)) ===\n"
                    + (f"--- {resources} ---\n" if resources else "")
                )
                for i, stack in enumerate(samples, 1):
                    fh.write(f"--- sample {i} ---\n{stack}")
        except OSError:
            pass


if __name__ == "__main__":
    # ponytail: self-check — a 1.5s blocking call on the loop is caught with its stack.
    import tempfile

    async def main() -> None:
        log = Path(tempfile.mkdtemp()) / "freeze.log"
        watch = StallWatch(asyncio.get_running_loop(), threshold=0.5, log_path=log)
        watch.start()
        await asyncio.sleep(0.6)
        busy = threading.Event()

        def spin() -> None:
            while not busy.is_set():
                sum(range(10_000))

        threading.Thread(target=spin, name="busy-worker", daemon=True).start()
        time.sleep(1.5)  # the freeze
        busy.set()
        await asyncio.sleep(0.5)
        watch.stop()
        assert watch.stalls, "stall not detected"
        seconds, samples = watch.stalls[0]
        assert seconds >= 1.0 and any("time.sleep(1.5)" in s for s in samples), samples
        text = log.read_text(encoding="utf-8")
        assert "UI loop frozen" in text and "--- cpu " in text, text[:400]
        assert "[busy-worker]" in text, "the other working thread is named"
        print(f"stall_watch self-check ok: caught {seconds:.1f}s stall")

    asyncio.run(main())
