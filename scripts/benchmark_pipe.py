"""Measure real pipe readiness and a deterministic first response, without a provider.

Uses the stub boundary in test_pipe_mode.py. Run under benchmark_startup.py
--suite pipe to collect repeated process-tree CPU and peak memory as well.
"""

from __future__ import annotations

# Fixed local subprocess and standalone JSON reporting.
# ruff: noqa: INP001, T201, S603
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time

_BOOTSTRAP = """
import sys
from novacode_cli import main as cli, ui_events as ev
from novacode_cli.headless.pipe import run_pipe_session
import novacode_cli.sessions.worker as worker
cli.check_cli_dependencies = lambda: None
cli.settings.get_onboarding_status = lambda: True
async def start(agent, state, *args, **kwargs):
    async def events(text, *args, **kwargs):
        yield ev.AssistantMessage(text, "Nova", "blue")
        yield ev.Done(True)
    worker.iterate_agent_events = events
    state.headless_exit_code = await run_pipe_session(
        agent=object(), assistant_id="benchmark", session_state=state)
cli.main = start
sys.argv = ["nova", "--mode", "pipe"]
cli.cli_main()
"""


def measure() -> dict:
    """Time protocol records as they arrive through an actual OS pipe."""
    received = queue.Queue(maxsize=32)
    started = time.perf_counter()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [sys.executable, "-c", _BOOTSTRAP],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )

        def reader() -> None:
            for line in process.stdout:
                received.put((time.perf_counter(), json.loads(line)))

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        report = {"provider": "deterministic stub", "first_visible_ms": None}
        try:
            ready_at, record = received.get(timeout=60)
            if record["type"] != "ready":
                message = "Expected ready protocol record"
                raise RuntimeError(message)
            report["readiness_ms"] = (ready_at - started) * 1000
            prompt_at = time.perf_counter()
            process.stdin.write(b'{"type":"prompt","id":"bench","content":"benchmark"}\n')
            process.stdin.flush()
            while True:
                timestamp, record = received.get(timeout=60)
                if record["type"] == "text":
                    report["first_visible_ms"] = (timestamp - prompt_at) * 1000
                if record["type"] == "done":
                    report["outcome"] = record.get("status")
                    break
            process.stdin.write(b'{"type":"shutdown"}\n')
            process.stdin.flush()
            process.wait(timeout=20)
            report["exit_code"] = process.returncode
            return report
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            thread.join(timeout=1)
            process.stdin.close()
            process.stdout.close()


if __name__ == "__main__":
    print(json.dumps(measure()))
