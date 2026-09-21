"""Daemon tool hardening — the failure modes a code review found.

The daemon subsystem is careful about the single-session case and assumed it
away exactly where the feature's purpose demands otherwise: the registry
outlives the process, so a pid is not an identity, another session may be
writing, and an agent-chosen name becomes a filesystem path. One test per
finding, each written to fail against the original code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import novacode_cli.daemons.registry as reg
from novacode_cli.tools import daemon_tool


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "DAEMONS_DIR", tmp_path)
    monkeypatch.setattr(reg, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(daemon_tool, "log_path_for", reg.log_path_for)


def _call(**kw) -> str:
    return daemon_tool.daemon.invoke(kw)


# ── 1. The approval gate ────────────────────────────────────────────────────
#
# `daemon` runs an arbitrary command with shell=True. It was absent from
# INTERRUPT_SPECS, and interrupt_on is built by iterating that dict — so the
# tool was never gated at all: `daemon(action="start", command=...)` executed
# unprompted what `shell(command=...)` would have stopped to ask about, and the
# process outlived the session. Absence failed OPEN.


def test_command_running_tools_are_gated():
    from novacode_cli.hitl.interrupts import INTERRUPT_SPECS

    for tool_name in ("shell", "execute", "daemon", "python_kernel"):
        assert tool_name in INTERRUPT_SPECS, f"{tool_name} bypasses the approval gate"


def test_a_daemon_command_is_judged_by_the_same_rules_as_shell():
    """Otherwise `daemon start` is a hole straight through the shell policy."""
    from novacode_cli.security.policy import get_policy

    policy = get_policy()
    dangerous = "rm -rf /"
    via_shell = policy.evaluate("shell", {"command": dangerous})
    via_daemon = policy.evaluate(
        "daemon", {"action": "start", "name": "x", "command": dangerous}
    )
    assert via_shell.tier == "deny"
    assert via_daemon.tier == "deny", "daemon bypassed the shell denylist"
    assert via_daemon.rule == via_shell.rule


def test_starting_and_stopping_still_ask_but_reading_does_not():
    """Gating the read-only actions would only train blanket approval."""
    from novacode_cli.security.policy import get_policy

    policy = get_policy()
    assert policy.evaluate("daemon", {"action": "start", "command": "npm run dev"}).tier == "ask"
    assert policy.evaluate("daemon", {"action": "stop", "name": "x"}).tier == "ask"
    for readonly in ("list", "status", "logs"):
        decision = policy.evaluate("daemon", {"action": readonly, "name": "x"})
        assert decision.tier == "allow", f"{readonly} should not prompt"


# ── 2. A name must not become a path ────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["../../../pwned", "..\\..\\pwned", "sub/../../x", "..", "...", "a/b/c"]
)
def test_a_daemon_name_cannot_escape_the_daemons_directory(name, tmp_path):
    path = reg.log_path_for(name).resolve()
    assert tmp_path.resolve() in path.parents or path.parent == tmp_path.resolve(), (
        f"{name!r} wrote to {path}, outside the daemons dir"
    )


def test_safe_name_keeps_ordinary_names_readable():
    assert reg.safe_name("web-server_2.0") == "web-server_2.0"
    assert reg.safe_name("") == "unnamed"
    assert reg.safe_name("...") == "unnamed"


# ── 3. A pid is not an identity ─────────────────────────────────────────────


def _info(**kw) -> reg.DaemonInfo:
    base = dict(
        name="d", pid=os.getpid(), command="c", log_path="l",
        started_at=time.time(), create_time=0.0,
    )
    base.update(kw)
    return reg.DaemonInfo(**base)


def test_a_recycled_pid_is_not_mistaken_for_our_daemon():
    """The headline risk: `stop` would force-kill an unrelated process tree."""
    live_but_not_ours = _info(pid=os.getpid(), create_time=1.0)  # 1970
    assert reg.is_alive(live_but_not_ours.pid), "precondition: the pid is live"
    assert not reg.is_ours(live_but_not_ours), "a stranger's pid read as our daemon"


def test_a_matching_creation_time_is_recognised():
    actual = reg.process_created_at(os.getpid())
    if actual is None:
        pytest.skip("platform cannot report process creation time")
    assert reg.is_ours(_info(pid=os.getpid(), create_time=actual))


def test_an_entry_without_a_creation_time_falls_back_to_liveness():
    """Entries written before this field existed must keep working."""
    assert reg.is_ours(_info(pid=os.getpid(), create_time=0.0))
    assert not reg.is_ours(_info(pid=999_999_999, create_time=0.0))


def test_is_alive_is_conservative_about_the_unknown():
    assert reg.is_alive(os.getpid())
    assert not reg.is_alive(0)
    assert not reg.is_alive(-1)


def test_stop_refuses_to_kill_a_process_that_is_not_ours(tmp_path):
    """It must remove the stale entry WITHOUT signalling the stranger."""
    reg.register(_info(name="ghost", pid=os.getpid(), create_time=1.0))
    killed: list = []
    original = subprocess.run

    def _spy(*a, **kw):
        killed.append(a)
        return original(*a, **kw)

    import novacode_cli.tools.daemon_tool as dt

    dt.subprocess.run = _spy
    try:
        out = _call(action="stop", name="ghost")
    finally:
        dt.subprocess.run = original

    assert "different process" in out
    assert killed == [], "signalled a process that was not our daemon"
    assert reg.get("ghost") is None, "the stale entry was left behind"
    assert os.getpid() > 0  # still here


# ── 4. Concurrent sessions share this registry ──────────────────────────────


def test_concurrent_processes_do_not_lose_each_others_daemons(tmp_path):
    """Measured on the original code: 43 of 80 entries lost, plus a crash.

    The lock was a threading.RLock and the temp file had one fixed name, so two
    Nova sessions raced on read-modify-write and on os.replace itself.
    """
    script = tmp_path / "writer.py"
    script.write_text(
        "import sys, time\n"
        # The package's own location, not cwd: another test in the same run can
        # leave the working directory elsewhere, and then the child process
        # cannot import novacode_cli at all.
        f"sys.path.insert(0, {str(Path(reg.__file__).resolve().parents[2])!r})\n"
        "from pathlib import Path\n"
        "import novacode_cli.daemons.registry as reg\n"
        f"reg.DAEMONS_DIR = Path({str(tmp_path)!r})\n"
        f"reg.REGISTRY_PATH = Path({str(tmp_path)!r}) / 'registry.json'\n"
        "tag = sys.argv[1]\n"
        "for i in range(30):\n"
        "    reg.register(reg.DaemonInfo(name=f'{tag}-{i}', pid=1, command='c',\n"
        "                                log_path='l', started_at=time.time()))\n",
        encoding="utf-8",
    )
    procs = [
        subprocess.Popen([sys.executable, str(script), tag]) for tag in ("A", "B")
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0, "a concurrent writer crashed"

    data = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert len(data) == 60, f"lost {60 - len(data)} entries to a cross-process race"


def test_a_stale_lock_does_not_wedge_the_registry(monkeypatch, tmp_path):
    """A session killed mid-write must not block every later one forever."""
    monkeypatch.setattr(reg, "_LOCK_TIMEOUT", 0.2)
    reg._lock_path().parent.mkdir(parents=True, exist_ok=True)
    reg._lock_path().write_text("held by a process that died", encoding="utf-8")

    reg.register(_info(name="after-stale"))
    assert reg.get("after-stale") is not None


def test_a_corrupt_registry_does_not_silently_erase_the_others():
    """_read swallows a JSONDecodeError and returns {} — so a truncated file
    used to make the next register() write a registry of one."""
    reg.register(_info(name="first"))
    assert reg.get("first") is not None
    reg.REGISTRY_PATH.write_text('{"first": {"name": "fir', encoding="utf-8")
    # Recovery is best-effort, but it must not raise.
    reg.register(_info(name="second"))
    assert reg.get("second") is not None


# ── 5. Reading a log must not read the whole log ────────────────────────────


def test_tailing_a_huge_log_reads_a_bounded_window(tmp_path):
    """Daemon logs are append-only and never rotated."""
    log = tmp_path / "big.log"
    line = "x" * 1000 + "\n"
    with log.open("w", encoding="utf-8") as fh:
        for i in range(3000):  # ~3 MB
            fh.write(f"{i:06d} {line}")
    assert log.stat().st_size > daemon_tool._TAIL_MAX_BYTES

    out = daemon_tool._tail(log, 5)
    assert len(out.splitlines()) == 5
    assert "002999" in out, "did not return the END of the log"
    assert len(out) < 20_000, "returned far more than the requested tail"


def test_tail_handles_small_and_empty_logs(tmp_path):
    empty = tmp_path / "e.log"
    empty.write_text("", encoding="utf-8")
    assert "empty" in daemon_tool._tail(empty)
    assert "no log file yet" in daemon_tool._tail(tmp_path / "missing.log")
    small = tmp_path / "s.log"
    small.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert daemon_tool._tail(small, 2).splitlines() == ["two", "three"]


# ── 6. Stopping reports the truth ───────────────────────────────────────────


def test_stop_keeps_the_entry_when_the_process_survives(monkeypatch):
    """Unregistering a live daemon makes it permanently untrackable."""
    reg.register(_info(name="stubborn", pid=os.getpid()))
    monkeypatch.setattr(reg, "is_ours", lambda info: True)
    monkeypatch.setattr(daemon_tool, "is_ours", lambda info: True)
    monkeypatch.setattr(daemon_tool, "is_alive", lambda pid: True)  # never dies
    monkeypatch.setattr(
        daemon_tool.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    monkeypatch.setattr(daemon_tool.os, "kill", lambda *a: None)
    monkeypatch.setattr(daemon_tool.os, "killpg", lambda *a: None, raising=False)
    monkeypatch.setattr(daemon_tool.os, "getpgid", lambda p: p, raising=False)

    out = _call(action="stop", name="stubborn")
    assert "still running" in out
    assert reg.get("stubborn") is not None, "unregistered a process still running"


def test_stopping_an_already_dead_daemon_cleans_up():
    reg.register(_info(name="dead", pid=999_999_999, create_time=0.0))
    out = _call(action="stop", name="dead")
    assert "not running" in out
    assert reg.get("dead") is None
