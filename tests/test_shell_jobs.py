"""Foreground shell control: Esc must kill a hung command promptly.

The shell tool runs its subprocess in a detached thread+loop that Textual task
cancellation can't reach, so before this a hung command froze the UI for the
full timeout (and could crash on force-quit). ``shell.jobs`` publishes a control
the TUI sets on Esc; the middleware read loop polls it and terminates.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time

import pytest

from novacode_cli.shell import jobs
from novacode_cli.shell.middleware import ShellMiddleware


def test_jobs_slot_lifecycle() -> None:
    assert jobs.get_current() is None
    assert jobs.request_kill() is False
    c = jobs.set_current("sleep 100")
    assert jobs.get_current() is c
    assert jobs.request_kill() is True and c.kill.is_set()
    assert jobs.request_detach() is False  # kill wins
    jobs.clear_current(c)
    assert jobs.get_current() is None


def test_background_logs_are_bounded_by_size_as_well_as_chunk_count() -> None:
    registry = jobs.JobRegistry()
    job = registry.add("noisy", "shell")
    for _ in range(1000):
        registry.append_log(job.id, "x" * 16384)
    assert len(job.logs) < 1000
    assert len(job.output) <= jobs._LOG_MAX_CHARS

    job.logs.extend(["old" * 100000, "latest"])
    assert job.output.endswith("latest")
    assert len(job.output) <= jobs._LOG_MAX_CHARS


@pytest.mark.timeout(30)
def test_kill_terminates_hung_command_promptly() -> None:
    """A 60s-hung command must die within a few seconds once kill is requested —
    not after the middleware's (here 120s) timeout."""
    mw = ShellMiddleware(workspace_root=os.getcwd(), timeout=120.0)
    # Run via `prog` (create_subprocess_exec) so a spaced interpreter path never
    # trips shell quoting — a reliable 60s hang on any platform.
    prog = [sys.executable, "-c"]
    cmd = "import time; time.sleep(60)"

    def kill_soon() -> None:
        for _ in range(100):  # wait up to 10s for the control to be published
            if jobs.get_current() is not None:
                break
            time.sleep(0.1)
        time.sleep(0.3)
        jobs.request_kill()

    t = threading.Thread(target=kill_soon)
    t.start()
    start = time.time()
    msg = asyncio.run(mw._async_local_shell(cmd, tool_call_id="t1", prog=prog))
    elapsed = time.time() - start
    t.join()

    assert elapsed < 15, f"kill should be prompt, took {elapsed:.1f}s"
    assert msg.status == "error"
    assert "killed by user" in (msg.content or "")
    assert jobs.get_current() is None  # cleared in finally


@pytest.mark.timeout(40)
def test_detach_backgrounds_then_job_completes() -> None:
    """Ctrl+B detach: the tool returns '[backgrounded: job N]' promptly while the
    command keeps running on the shared background loop and completes with its
    full output + exit code (firing the completion callback)."""
    mw = ShellMiddleware(workspace_root=os.getcwd(), timeout=30.0)
    prog = [sys.executable, "-u", "-c"]
    code = "import time\nfor i in range(4):\n print('line', i, flush=True)\n time.sleep(1)"

    reg = jobs.get_registry()
    completed: list[object] = []
    reg.set_completion_callback(completed.append)

    box: dict[str, object] = {}

    def run() -> None:
        box["msg"] = mw._run_local_command(code, tool_call_id="d", prog=prog)

    th = threading.Thread(target=run)
    th.start()
    for _ in range(100):  # wait for the command to publish
        if jobs.get_current() is not None:
            break
        time.sleep(0.1)
    time.sleep(1.2)
    assert jobs.request_detach() is True
    th.join(timeout=10)

    msg = box["msg"]
    assert "backgrounded: task_" in msg.content  # returned before the command ended

    m = re.search(r"backgrounded: task_(\d+)", msg.content)
    assert m, msg.content
    task_id = int(m.group(1))

    # Wait for OUR job specifically. The registry is process-global and its
    # reset() drops references without stopping work already in flight, so a job
    # leaked from an earlier test can complete here and fire this callback first
    # — completed[0] was then a foreign 'terminated' job under full-suite load.
    job = None
    for _ in range(120):
        job = next((j for j in completed if j.id == task_id), None)
        if job is not None:
            break
        time.sleep(0.1)
    assert job is not None, f"completion callback never fired for task_{task_id}"
    assert job.status == "done"
    assert job.exit_code == 0
    assert "line 3" in job.output  # full output captured after detach

    reg.set_completion_callback(None)  # don't leak the callback into other tests


@pytest.mark.timeout(40)
def test_launch_terminate_and_restart_background_task() -> None:
    """Launch a background task, stream its logs, terminate it (process-group
    kill), then restart it as a fresh task."""
    mw = ShellMiddleware(workspace_root=os.getcwd(), timeout=30.0)
    reg = jobs.get_registry()
    prog = [sys.executable, "-u", "-c"]
    code = "import time\nwhile True:\n print('tick', flush=True)\n time.sleep(0.3)"

    job = mw._launch_background(code, prog)
    assert job.status == "running" and job.task_id.startswith("task_")

    for _ in range(60):  # live logs stream in
        if "tick" in job.output:
            break
        time.sleep(0.1)
    assert "tick" in job.output

    assert reg.terminate(job.id) is True
    for _ in range(80):  # drain loop kills the tree and marks terminated
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status == "terminated"

    j2 = reg.restart(job.id)
    assert j2 is not None and j2.id != job.id and j2.status == "running"
    reg.terminate(j2.id)  # cleanup
    for _ in range(80):
        if j2.status != "running":
            break
        time.sleep(0.1)
    assert j2.status == "terminated"


def test_execute_tool_is_intercepted_for_background_parity() -> None:
    """The deepagents `execute` tool (local mode) is routed through Nova's shell
    path so it gets background/Ctrl+B parity; other tools pass through; a
    long-running server started via execute becomes a background task."""
    mw = ShellMiddleware(workspace_root=os.getcwd(), timeout=30.0)  # local (no sandbox)

    class _Req:
        def __init__(self, name, **args):
            self.tool_call = {"name": name, "id": "c1", "args": args}

    # non-execute tool passes through to the handler untouched
    seen = {"h": False}

    def handler(_req):
        seen["h"] = True
        return "HANDLER"

    assert mw.wrap_tool_call(_Req("read_file", file_path="x"), handler) == "HANDLER"
    assert seen["h"]

    # execute is intercepted (handler NOT called) and runs locally
    seen["h"] = False
    msg = mw.wrap_tool_call(_Req("execute", command="echo hi-exec"), handler)
    assert not seen["h"]
    assert "hi-exec" in getattr(msg, "content", str(msg))

    # a server started via execute becomes a tracked background task
    import re

    msg2 = mw.wrap_tool_call(
        _Req("execute", command="python -m http.server 8131 --bind 127.0.0.1"), handler
    )
    m = re.search(r"task_\d+", getattr(msg2, "content", str(msg2)))
    assert m, "server via execute should background into a task"
    job = jobs.get_registry().resolve(m.group(0))
    assert job is not None and job.status == "running"
    jobs.get_registry().terminate(job.id)  # cleanup


def test_task_logs_cursor_returns_only_new_output():
    from novacode_cli.shell import jobs
    from novacode_cli.tools.job_tools import get_task_logs

    registry = jobs.get_registry()
    job = registry.add("build", "shell", None)
    registry.append_log(job.id, "line 1\nline 2\n")
    first = get_task_logs.invoke({"task_id": job.task_id})
    assert first.endswith("[next from_offset=14]")
    registry.append_log(job.id, "line 3\n")
    new = get_task_logs.invoke({"task_id": job.task_id, "from_offset": 14})
    assert "line 3" in new
    assert "line 1" not in new
    assert new.endswith("[next from_offset=21]")
    assert "no new output" in get_task_logs.invoke({"task_id": job.task_id, "from_offset": 21})
