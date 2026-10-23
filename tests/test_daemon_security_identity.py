"""Never kill a live PID on liveness alone, including legacy registry entries."""

import os
import time
from unittest.mock import Mock

import pytest

from novacode_cli.daemons import registry as reg
from novacode_cli.tools import daemon_tool


@pytest.mark.parametrize("missing_recorded_time", [True, False])
def test_stop_keeps_unknown_identity_without_signalling(
    tmp_path, monkeypatch, missing_recorded_time
):
    monkeypatch.setattr(reg, "DAEMONS_DIR", tmp_path)
    monkeypatch.setattr(reg, "REGISTRY_PATH", tmp_path / "registry.json")
    created = reg.process_created_at(os.getpid()) or 1.0
    reg.register(
        reg.DaemonInfo(
            "unknown",
            os.getpid(),
            "dummy",
            "dummy.log",
            time.time(),
            create_time=0.0 if missing_recorded_time else created,
        )
    )
    killed = Mock()
    monkeypatch.setattr(daemon_tool.subprocess, "run", killed)
    monkeypatch.setattr(daemon_tool.os, "killpg", killed, raising=False)
    monkeypatch.setattr(daemon_tool.os, "kill", killed)
    monkeypatch.setattr(daemon_tool, "is_alive", lambda _: True)
    monkeypatch.setattr(daemon_tool.time, "sleep", lambda _: None)
    if not missing_recorded_time:
        monkeypatch.setattr(reg, "process_created_at", lambda _: None)
        monkeypatch.setattr(daemon_tool, "process_created_at", lambda _: None)
    result = daemon_tool.daemon.invoke({"action": "stop", "name": "unknown"})
    assert "identity" in result.lower()
    killed.assert_not_called()
    assert reg.get("unknown") is not None


def test_creation_time_reuse_within_two_seconds_is_rejected(monkeypatch):
    monkeypatch.setattr(reg, "is_alive", lambda _: True)
    monkeypatch.setattr(reg, "process_created_at", lambda _: 100.5)
    info = reg.DaemonInfo("old", 1234, "command", "log", 100.0, create_time=100.0)
    assert not reg.is_ours(info)


@pytest.mark.skipif(os.name != "nt", reason="Windows executable lookup")
def test_stop_uses_system_taskkill_not_project_or_path(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    monkeypatch.setattr(reg, "DAEMONS_DIR", tmp_path)
    monkeypatch.setattr(reg, "REGISTRY_PATH", tmp_path / "registry.json")
    created = reg.process_created_at(os.getpid())
    assert created
    reg.register(
        reg.DaemonInfo("safe", os.getpid(), "dummy", "dummy.log", time.time(), create_time=created)
    )
    monkeypatch.setattr(daemon_tool, "is_ours", lambda _: True)
    alive = iter([True, False])
    monkeypatch.setattr(daemon_tool, "is_alive", lambda _: next(alive))
    called = []

    def run(argv, **kwargs):
        called.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(daemon_tool.subprocess, "run", run)
    assert "Stopped daemon" in daemon_tool.daemon.invoke({"action": "stop", "name": "safe"})
    executable = Path(called[0][0])
    assert executable.is_absolute()
    assert executable == Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
