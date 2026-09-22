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
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import os

LOG_PATH = Path(os.environ.get("NOVA_FREEZE_LOG") or Path.home() / ".nova" / "logs" / "freeze.log")
_MAX_LOG_BYTES = 2_000_000
_PING_EVERY = 0.25
_SAMPLE_EVERY = 1.0


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
        self.stalls: list[tuple[float, list[str]]] = []  # (seconds, stack samples)

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
        frame = sys._current_frames().get(self._loop_thread)
        if frame is None:
            return "(loop thread gone)"
        return "".join(traceback.format_stack(frame))  # whole stack: the caller matters

    def _run(self) -> None:
        while not self._stop.is_set():
            answered = threading.Event()
            started = time.monotonic()
            try:
                self._loop.call_soon_threadsafe(answered.set)
            except RuntimeError:  # loop closed: the app is gone
                return
            if answered.wait(self._threshold):
                self._stop.wait(_PING_EVERY)
                continue
            samples: list[str] = []
            while not answered.wait(0 if not samples else _SAMPLE_EVERY):
                stack = self._sample()
                if stack not in samples:
                    samples.append(stack)
                if self._stop.is_set():
                    return
            self._record(time.monotonic() - started, samples)

    def _record(self, seconds: float, samples: list[str]) -> None:
        self.stalls.append((seconds, samples))
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_path.exists() and self._log_path.stat().st_size > _MAX_LOG_BYTES:
                self._log_path.replace(self._log_path.with_suffix(".log.1"))
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} UI loop frozen "
                    f"{seconds:.1f}s ({len(samples)} distinct stack(s)) ===\n"
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
        time.sleep(1.5)  # the freeze
        await asyncio.sleep(0.5)
        watch.stop()
        assert watch.stalls, "stall not detected"
        seconds, samples = watch.stalls[0]
        assert seconds >= 1.0 and any("time.sleep(1.5)" in s for s in samples), samples
        assert "UI loop frozen" in log.read_text(encoding="utf-8")
        print(f"stall_watch self-check ok: caught {seconds:.1f}s stall")

    asyncio.run(main())
