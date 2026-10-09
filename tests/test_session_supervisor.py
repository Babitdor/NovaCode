"""Parent-side management of spawned session processes.

These run REAL subprocesses over REAL pipes — just against a ~20-line fake child
instead of a full Nova, so no model, API key, or agent build is involved. That
keeps the thing under test honest (asyncio pipe handling, EOF, exit codes,
Windows process semantics) while staying fast.

The contract pinned here:

* frames from the child reach the app callback, and drive session status;
* prompts and approval replies get down the pipe;
* a crash resolves every pending approval future — otherwise the parent would
  await a decision that can never arrive, hanging that tab forever;
* close() shuts a child down and escalates if it ignores the request;
* a child that ignores `shutdown` is still gone after close().

Runnable directly (``python tests/test_session_supervisor.py``) or via pytest.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

import pytest

from novacode_cli.sessions.supervisor import SessionSupervisor

# ── fake children ────────────────────────────────────────────────────────────

# Echoes prompts back as AssistantMessage events, and raises one interrupt when
# asked. Reads JSONL on stdin, writes JSONL on stdout — the real protocol.
_ECHO_CHILD = r"""
import json, sys
def out(m): sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
out({"t": "ready", "session_id": "s1", "thread_id": "t1", "cwd": "."})
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    t = msg.get("t")
    if t == "shutdown":
        break
    if t == "prompt":
        text = msg.get("text", "")
        if text == "ASK":
            out({"t": "interrupt", "id": "i1", "kind": "tool", "payload": {"q": 1}})
            continue
        out({"t": "ev", "c": "AssistantMessage",
             "d": {"text": text, "agent_name": "nova", "agent_color": "cyan",
                   "is_subagent": False}})
        out({"t": "turn_done", "id": msg.get("id"), "ok": True})
    if t == "interrupt_reply":
        out({"t": "ev", "c": "AssistantMessage",
             "d": {"text": "decided:" + json.dumps(msg.get("result")),
                   "agent_name": "nova", "agent_color": "cyan", "is_subagent": False}})
        out({"t": "turn_done", "id": "i1", "ok": True})
sys.exit(0)
"""

# Dies immediately with a non-zero code and a diagnostic on stderr.
_CRASH_CHILD = r"""
import sys
sys.stderr.write("boom: could not start\n")
sys.exit(3)
"""

# Emits ready then ignores everything, including shutdown.
_STUBBORN_CHILD = r"""
import json, sys, time
sys.stdout.write(json.dumps({"t": "ready"}) + "\n"); sys.stdout.flush()
time.sleep(120)
"""


def _argv(src: str) -> list[str]:
    return [sys.executable, "-c", src]


class _Collector:
    """Captures (session_id, message) pairs the supervisor delivers."""

    def __init__(self) -> None:
        self.msgs: list[tuple[str, dict]] = []

    async def __call__(self, sid: str, msg: dict) -> None:
        self.msgs.append((sid, msg))

    def kinds(self) -> list[str]:
        return [m.get("t") for _, m in self.msgs]

    def of(self, kind: str) -> list[dict]:
        return [m for _, m in self.msgs if m.get("t") == kind]


@pytest.mark.asyncio
async def test_concurrent_launches_reserve_capacity_and_release_on_failure(monkeypatch, tmp_path):
    from novacode_cli.sessions.supervisor import MAX_SESSIONS
    entered = []
    gate = asyncio.Event()

    async def launch(*args, **kwargs):
        entered.append(args)
        await gate.wait()
        raise OSError("launch failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    supervisor = SessionSupervisor(_Collector())
    tasks = [asyncio.create_task(supervisor.spawn(session_id=str(i), name=str(i), worktree=tmp_path))
             for i in range(MAX_SESSIONS + 2)]
    await asyncio.sleep(0)
    assert len(entered) == MAX_SESSIONS
    assert supervisor.at_capacity()
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(r, RuntimeError) for r in results) == 2
    assert not supervisor._starting
    assert not supervisor.at_capacity()


@pytest.mark.asyncio
async def test_cancelled_launch_releases_reservation(monkeypatch, tmp_path):
    async def launch(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    supervisor = SessionSupervisor(_Collector())
    task = asyncio.create_task(supervisor.spawn(session_id="cancelled", name="cancelled", worktree=tmp_path))
    await asyncio.sleep(0)
    assert supervisor._starting == {"cancelled"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not supervisor._starting


async def _wait_for(pred, *, timeout=10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def sup_and_child(tmp_path):
    """A supervisor plus cleanup, so a hung child can never stall the suite."""
    made: list[SessionSupervisor] = []

    def factory():
        c = _Collector()
        s = SessionSupervisor(c)
        made.append(s)
        return s, c

    yield factory

    # Always tear children down, even on test failure — a surviving child would
    # hold pipes open and stall the rest of the suite.
    for s in made:
        with contextlib.suppress(Exception):
            await s.close_all(timeout=3.0)


# ── lifecycle ────────────────────────────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_spawn_receives_ready(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    child = await sup.spawn(
        session_id="s1", name="one", worktree=tmp_path, argv=_argv(_ECHO_CHILD)
    )
    assert await _wait_for(lambda: coll.of("ready"))
    assert child.status == "idle"
    assert sup.get("s1") is child
    assert [c.session_id for c in sup.list()] == ["s1"]


@pytest.mark.timeout(60)
async def test_prompt_round_trips_as_events(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    await sup.spawn(session_id="s1", name="one", worktree=tmp_path, argv=_argv(_ECHO_CHILD))
    assert await _wait_for(lambda: coll.of("ready"))

    pid = await sup.send_prompt("s1", "hello there")
    assert pid is not None
    assert await _wait_for(lambda: coll.of("turn_done"))

    from novacode_cli.sessions import protocol

    texts = [protocol.decode_event(m).text for m in coll.of("ev")]
    assert "hello there" in texts
    assert coll.of("turn_done")[0]["id"] == pid
    assert sup.get("s1").status == "idle"


@pytest.mark.timeout(60)
async def test_child_runs_in_its_worktree(sup_and_child, tmp_path):
    """cwd is the whole worktree binding, so it must actually be applied."""
    work = tmp_path / "wt"
    work.mkdir()
    src = (
        "import json,os,sys\n"
        "sys.stdout.write(json.dumps({'t':'ready','cwd':os.getcwd()})+'\\n')\n"
        "sys.stdout.flush()\n"
    )
    sup, coll = sup_and_child()
    await sup.spawn(session_id="s1", name="one", worktree=work, argv=_argv(src))
    assert await _wait_for(lambda: coll.of("ready"))

    from pathlib import Path

    assert Path(coll.of("ready")[0]["cwd"]).resolve() == work.resolve()


@pytest.mark.timeout(60)
async def test_status_tracks_activity(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    child = await sup.spawn(
        session_id="s1", name="one", worktree=tmp_path, argv=_argv(_ECHO_CHILD)
    )
    assert await _wait_for(lambda: child.status == "idle")
    await sup.send_prompt("s1", "work")
    assert await _wait_for(lambda: child.status == "idle" and coll.of("turn_done"))


# ── the child must run THIS Nova, not the worktree's checkout ────────────────


def test_child_env_pins_the_parents_package():
    """A spawned session must import novacode_cli from the PARENT install.

    The child runs with ``cwd=<worktree>`` so it resolves its project root
    there, but ``python -m`` also puts CWD first on sys.path — so it would
    import ``novacode_cli`` from the worktree, which is a checkout of HEAD and
    therefore a different, older Nova. That is not hypothetical: every spawned
    session died instantly with ``error: argument command: invalid choice``
    because the worktree's copy had no ``--session-worker`` flag.
    """
    import novacode_cli

    env = SessionSupervisor._child_env()
    pkg_root = str(Path(novacode_cli.__file__).resolve().parent.parent)

    # CWD must not win over the parent's package.
    assert env.get("PYTHONSAFEPATH") == "1"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == pkg_root
    assert env.get("PYTHONIOENCODING") == "utf-8"


def test_child_env_preserves_existing_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/some/existing/path")
    env = SessionSupervisor._child_env()
    assert env["PYTHONPATH"].endswith("/some/existing/path")
    assert os.pathsep in env["PYTHONPATH"]


# ── approvals ────────────────────────────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_interrupt_marks_pending_and_reply_clears_it(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    child = await sup.spawn(
        session_id="s1", name="one", worktree=tmp_path, argv=_argv(_ECHO_CHILD)
    )
    assert await _wait_for(lambda: coll.of("ready"))

    await sup.send_prompt("s1", "ASK")
    assert await _wait_for(lambda: coll.of("interrupt"))
    assert child.status == "needs-approval"
    assert "i1" in child.pending

    await sup.reply_interrupt("s1", "i1", {"decisions": [{"type": "approve"}]})
    assert await _wait_for(lambda: coll.of("turn_done"))
    assert "i1" not in child.pending
    assert child.status != "needs-approval"

    from novacode_cli.sessions import protocol

    texts = [protocol.decode_event(m).text for m in coll.of("ev")]
    assert any(t.startswith("decided:") for t in texts)


# ── crashes ──────────────────────────────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_crash_is_reported_with_stderr(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    child = await sup.spawn(
        session_id="s1", name="one", worktree=tmp_path, argv=_argv(_CRASH_CHILD)
    )
    assert await _wait_for(lambda: coll.of("exited"))

    assert child.status == "crashed"
    assert child.exit_code == 3
    errs = coll.of("error")
    assert errs and "boom" in errs[0]["message"]
    assert coll.of("exited")[0]["crashed"] is True


@pytest.mark.timeout(60)
async def test_a_finished_child_is_reaped(sup_and_child, tmp_path):
    """A child that exits on its own must not stay tracked forever.

    Only close() popped from ``_children``, so a child that crashed on start,
    was killed externally, or simply finished stayed in the dict for the life
    of the parent — holding its process handle, reader tasks and stderr tail.
    at_capacity() filters on ``.alive``, so new spawns were still allowed and
    the leak was silent: sessions accumulated across a long run.
    """
    sup, coll = sup_and_child()
    for i in range(3):
        await sup.spawn(
            session_id=f"s{i}", name=f"n{i}", worktree=tmp_path,
            argv=_argv(_CRASH_CHILD),
        )

    assert await _wait_for(lambda: len(coll.of("exited")) == 3)
    # The exits were reported (the UI has what it needs) ...
    assert len(coll.of("exited")) == 3
    # ... and nothing dead is still held.
    assert await _wait_for(lambda: sup.list() == []), (
        f"{len(sup.list())} dead child(ren) still tracked"
    )


@pytest.mark.timeout(60)
async def test_reaping_frees_capacity_for_new_sessions(sup_and_child, tmp_path):
    """Dead children must not consume the MAX_SESSIONS budget."""
    from novacode_cli.sessions.supervisor import MAX_SESSIONS

    sup, coll = sup_and_child()
    for i in range(MAX_SESSIONS):
        await sup.spawn(
            session_id=f"c{i}", name=f"c{i}", worktree=tmp_path,
            argv=_argv(_CRASH_CHILD),
        )
    assert await _wait_for(lambda: len(coll.of("exited")) == MAX_SESSIONS)
    assert not sup.at_capacity(), "dead children still counted against capacity"
    assert await _wait_for(lambda: sup.list() == [])


@pytest.mark.timeout(60)
async def test_crash_resolves_pending_approvals(sup_and_child, tmp_path):
    """A dead child must never leave the parent awaiting a decision forever."""
    # The child holds briefly after raising the interrupt so the parent can
    # observe the pending future BEFORE the crash clears it. Exiting immediately
    # made this race under load: _on_exit had already emptied `pending`.
    src = (
        "import json,sys,time\n"
        "def out(m): sys.stdout.write(json.dumps(m)+'\\n'); sys.stdout.flush()\n"
        "out({'t':'ready'})\n"
        "out({'t':'interrupt','id':'i1','kind':'tool','payload':{}})\n"
        "time.sleep(1.5)\n"
        "sys.exit(2)\n"
    )
    sup, coll = sup_and_child()
    child = await sup.spawn(session_id="s1", name="one", worktree=tmp_path, argv=_argv(src))

    assert await _wait_for(lambda: coll.of("interrupt"))
    fut = child.pending.get("i1")
    assert fut is not None, "parent should be holding a pending approval here"

    assert await _wait_for(lambda: coll.of("exited"))
    assert fut.done(), "pending approval must be resolved when the child dies"
    assert not child.pending
    assert child.status == "crashed"


@pytest.mark.timeout(60)
async def test_send_to_dead_child_returns_none(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    await sup.spawn(session_id="s1", name="one", worktree=tmp_path, argv=_argv(_CRASH_CHILD))
    assert await _wait_for(lambda: coll.of("exited"))
    assert await sup.send_prompt("s1", "anyone there?") is None


@pytest.mark.timeout(60)
async def test_clean_exit_is_not_a_crash(sup_and_child, tmp_path):
    src = "import json,sys\nsys.stdout.write(json.dumps({'t':'ready'})+'\\n')\n"
    sup, coll = sup_and_child()
    child = await sup.spawn(session_id="s1", name="one", worktree=tmp_path, argv=_argv(src))
    assert await _wait_for(lambda: coll.of("exited"))
    assert child.exit_code == 0
    assert child.status == "exited"
    assert not coll.of("error")


# ── teardown ─────────────────────────────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_close_shuts_a_child_down(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    child = await sup.spawn(
        session_id="s1", name="one", worktree=tmp_path, argv=_argv(_ECHO_CHILD)
    )
    assert await _wait_for(lambda: coll.of("ready"))

    closed = await sup.close("s1")
    assert closed is child
    assert not child.alive
    assert sup.get("s1") is None


@pytest.mark.timeout(60)
async def test_close_escalates_when_shutdown_is_ignored(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    child = await sup.spawn(
        session_id="s1", name="one", worktree=tmp_path, argv=_argv(_STUBBORN_CHILD)
    )
    assert await _wait_for(lambda: coll.of("ready"))

    await sup.close("s1", timeout=1.0)  # ignores shutdown -> terminate -> kill
    assert not child.alive


@pytest.mark.timeout(60)
async def test_close_all_closes_everything(sup_and_child, tmp_path):
    sup, coll = sup_and_child()
    for i in range(2):
        await sup.spawn(
            session_id=f"s{i}", name=f"n{i}", worktree=tmp_path, argv=_argv(_ECHO_CHILD)
        )
    assert await _wait_for(lambda: len(coll.of("ready")) == 2)

    closed = await sup.close_all()
    assert len(closed) == 2
    assert sup.list() == []


@pytest.mark.timeout(60)
async def test_close_unknown_session_is_harmless(sup_and_child, tmp_path):
    sup, _ = sup_and_child()
    assert await sup.close("nope") is None


# ── capacity ─────────────────────────────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_capacity_reflects_live_children(sup_and_child, tmp_path, monkeypatch):
    import novacode_cli.sessions.supervisor as mod

    monkeypatch.setattr(mod, "MAX_SESSIONS", 1)
    sup, coll = sup_and_child()
    assert not sup.at_capacity()
    await sup.spawn(session_id="s1", name="one", worktree=tmp_path, argv=_argv(_ECHO_CHILD))
    assert await _wait_for(lambda: coll.of("ready"))
    assert sup.at_capacity()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--assert=plain"]))
