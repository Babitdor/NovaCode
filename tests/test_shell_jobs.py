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
            from langchain_core.tools import StructuredTool

            self.tool = StructuredTool.from_function(
                lambda command: "backend", name="execute", description="Backend shell",
            )

        def override(self, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(tool_call=self.tool_call, **kwargs)

    # non-execute tool passes through to the handler untouched
    seen = {"h": False}

    def handler(_req):
        seen["h"] = True
        if _req.tool_call["name"] == "execute":
            return _req.tool.invoke(_req.tool_call["args"])
        return "HANDLER"

    assert mw.wrap_tool_call(_Req("read_file", file_path="x"), handler) == "HANDLER"
    assert seen["h"]

    # execute retains the downstream handler and runs locally after its checks
    seen["h"] = False
    msg = mw.wrap_tool_call(_Req("execute", command="echo hi-exec"), handler)
    assert seen["h"]
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


def test_task_logs_offset_inside_a_split_escape_leaves_no_fragment():
    """A from_offset landing inside a split escape resumes after it closes.

    Regression: the raw slice starting mid-escape left an orphan fragment
    (e.g. "[31;1m") that strip_ansi cannot remove — its ESC is before the
    cut — so the page began with stray glyphs.
    """
    from novacode_cli.shell import jobs
    from novacode_cli.tools.job_tools import get_task_logs

    registry = jobs.get_registry()
    job = registry.add("build", "shell", None)
    registry.append_log(job.id, "plain\n")
    first = get_task_logs.invoke({"task_id": job.task_id})
    m = re.search(r"\[next from_offset=(\d+)\]", first)
    assert m
    # "colored\n" is appended as two chunks whose boundary splits the escape:
    # "\x1b[3" + "8;5;196mRED\x1b[0m\n"
    registry.append_log(job.id, "\x1b[3")
    registry.append_log(job.id, "8;5;196mRED\x1b[0m\n")
    raw = "".join(job.logs)
    cut = raw.find("\x1b[3") + 2  # inside the split sequence
    new = get_task_logs.invoke({"task_id": job.task_id, "from_offset": cut})
    assert "RED" in new
    assert not re.search(r"^\[?3?[;0-9]*m?RED", new.replace("\x1b", "")), new
    assert "[31;" not in new and "[3" + "8;" not in new
    registry.reset()


def test_split_escape_end_edges():
    from novacode_cli.shell.output import split_escape_end

    text = "abc\x1b[31;1mRED\x1b[0m\n"
    cut = text.find("31")  # inside the CSI sequence
    assert split_escape_end(text, cut) == text.find("m") + 1
    # No ESC before the cut → not inside a sequence.
    assert split_escape_end("plain text\n", 5) == -1
    # Sequence closed BEFORE the cut → not inside.
    assert split_escape_end(text, text.find("RED")) == -1
    # Unterminated sequence at the end → give up (-1), keep the fragment.
    assert split_escape_end("x\x1b[31;1", 3) == -1


def test_task_logs_cursor_matches_coloured_output():
    """Offsets and slices share one coordinate space even with ANSI escapes.

    Regression: ``from_offset`` cursors counted raw characters (escapes
    included) while the slice came from the ANSI-stripped string, so coloured
    output drifted the cursor and repeated or skipped lines between calls.
    """
    from novacode_cli.shell import jobs
    from novacode_cli.tools.job_tools import get_task_logs

    registry = jobs.get_registry()
    job = registry.add("build", "shell", None)
    registry.append_log(job.id, "\x1b[31mred line\x1b[0m\n")
    first = get_task_logs.invoke({"task_id": job.task_id})
    assert "red line" in first
    m = re.search(r"\[next from_offset=(\d+)\]", first)
    assert m
    registry.append_log(job.id, "plain line\n")
    new = get_task_logs.invoke({"task_id": job.task_id, "from_offset": int(m.group(1))})
    assert "plain line" in new
    assert "red line" not in new
    registry.reset()


def test_task_logs_tail_lines_zero_returns_whole_output():
    """tail_lines=0 means "no tail limit" — the whole (stripped) output.

    max(1, tail_lines) would have clamped 0 to 1 and returned only the last
    line; the no-limit path applies to both the plain and the from_offset
    slice, whose strip/slice coordinates already share the raw buffer.
    """
    from novacode_cli.shell import jobs
    from novacode_cli.tools.job_tools import get_task_logs

    registry = jobs.get_registry()
    job = registry.add("build", "shell", None)
    registry.append_log(job.id, "line 1\nline 2\nline 3\n")
    whole = get_task_logs.invoke({"task_id": job.task_id, "tail_lines": 0})
    assert "line 1" in whole and "line 2" in whole and "line 3" in whole
    assert "last 3 line(s)" in whole
    registry.append_log(job.id, "\x1b[32mgreen line\x1b[0m\n")
    m = re.search(r"\[next from_offset=(\d+)\]", whole)
    assert m
    new = get_task_logs.invoke(
        {"task_id": job.task_id, "from_offset": int(m.group(1)), "tail_lines": 0}
    )
    assert "green line" in new
    assert "line 1" not in new
    registry.reset()


def test_force_terminate_resolves_a_dead_drain_loop():
    """kill.set() alone leaves status running if the drain loop is gone.

    Regression: a job whose drain thread died before reacting to the kill
    event stayed 'running' forever — no wait resolved and terminate_task kept
    reporting success into the stuck state. force=True settles it.
    """
    registry = jobs.JobRegistry()
    job = registry.add("server", "execute")
    assert registry.terminate(job.id)
    assert job.status == "running"  # graceful: drain loop owns the transition
    registry2 = jobs.JobRegistry()
    stuck = registry2.add("server", "execute")
    assert registry2.terminate(stuck.id, force=True)
    assert stuck.status == "terminated"
    assert stuck._done.is_set()
    assert registry2.wait(stuck.id, timeout=0) is stuck


def test_a_background_job_reports_output_without_terminal_colour():
    """A sequence split across two reads is only whole once the log is joined."""
    registry = jobs.get_registry()
    job = registry.add("pytest", "shell", None)
    registry.append_log(job.id, "\x1b[31")
    registry.append_log(job.id, ";1m3 failed\x1b[0m\n")
    assert job.output == "3 failed\n"
    registry.reset()
