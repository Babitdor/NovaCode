"""An update must not mutate packages held by another running terminal."""

from __future__ import annotations

# Isolated native subprocesses; nested locks model independent owners.
# ruff: noqa: S603, SIM117
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from novacode_cli import update_guard as guard
from novacode_cli import updates


def test_blocker_prevents_even_launcher_or_package_mutation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(updates, "installation_lock", lambda **_kw: nullcontext())
    monkeypatch.setattr(
        updates, "check_update_blockers", Mock(side_effect=guard.UpdateBlockedError("busy"))
    )
    install = Mock()
    monkeypatch.setattr(updates, "_install_update", install)
    with pytest.raises(guard.UpdateBlockedError, match="busy"):
        updates.install_update()
    install.assert_not_called()


def test_blocked_clean_update_resumes_unchanged_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(updates.RESTART_SESSION_ENV, "saved")
    monkeypatch.setenv(updates.CONTINUE_ENV, "1")
    monkeypatch.setattr(updates, "_wait_for_handoff", lambda: None)
    monkeypatch.setattr(updates, "_update_with_progress", lambda **_kw: updates.EXIT_UPDATE_BLOCKED)
    relaunch = Mock(return_value=0)
    monkeypatch.setattr(updates, "_relaunch", relaunch)
    assert updates._update_after_exit() == 0
    relaunch.assert_called_once_with("saved", continue_turn=True)


def test_handoff_timeout_never_starts_installation(monkeypatch: pytest.MonkeyPatch):
    import ctypes

    kernel = Mock()
    kernel.OpenProcess.return_value = 123
    kernel.WaitForSingleObject.return_value = 258
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setenv(updates.HANDOFF_PID_ENV, "4242")
    install = Mock()
    monkeypatch.setattr(updates, "_update_with_progress", install)
    assert updates._update_after_exit() == updates.EXIT_UPDATE_BLOCKED
    install.assert_not_called()
    kernel.CloseHandle.assert_called_once_with(123)


async def test_tui_blocker_keeps_session_and_turn_running(monkeypatch: pytest.MonkeyPatch):
    from tests.test_tui_compaction_row import _app

    app = _app()
    # This focused preflight test does not mount a screen; status rendering is
    # covered by the update-button tests running a real Textual pilot.
    monkeypatch.setattr(app, "_set_status", Mock())
    monkeypatch.setattr(app, "notify", Mock())
    monkeypatch.setattr(
        guard, "check_update_blockers", Mock(side_effect=guard.UpdateBlockedError("PID 42"))
    )
    monkeypatch.setattr(updates, "restart_request", None)
    stopped, saved, exited, notices = Mock(), Mock(), Mock(), []
    monkeypatch.setattr(app, "_stop_main_turn", stopped)
    monkeypatch.setattr(app, "_save_session", saved)
    monkeypatch.setattr(app, "exit", exited)
    monkeypatch.setattr(app, "_log", lambda text: notices.append(text.plain))
    await app._update_and_restart()
    stopped.assert_not_called()
    saved.assert_not_called()
    exited.assert_not_called()
    assert updates.restart_request is None
    assert "PID 42" in notices[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native file mapping")
@pytest.mark.timeout(30)
def test_native_mapped_dependency_blocks_update_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    library = tmp_path / "dependency.pyd"
    shutil.copyfile(Path(os.environ["SYSTEMROOT"]) / "System32" / "version.dll", library)
    original = library.read_bytes()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import ctypes,os,sys; dll=ctypes.WinDLL(sys.argv[1]); "
            "print(os.getpid(),flush=True); sys.stdin.read(1)",
            str(library),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Windows venv python.exe may redirect to a different interpreter PID.
        owner_pid = int(child.stdout.readline().strip())
        with pytest.raises(PermissionError):
            library.unlink()  # The actual Windows failure that used to occur mid-install.
        assert owner_pid in {pid for pid, _ in guard.locked_processes([library])}
        monkeypatch.setattr(
            updates, "check_update_blockers", lambda: guard.check_update_blockers(root=tmp_path)
        )
        monkeypatch.setattr(
            updates, "installation_lock", lambda **kw: guard.installation_lock(root=tmp_path, **kw)
        )
        install = Mock(return_value=True)
        monkeypatch.setattr(updates, "_install_update", install)
        with pytest.raises(guard.UpdateBlockedError, match=str(owner_pid)):
            updates.install_update()
        install.assert_not_called()
        assert library.read_bytes() == original
        child.communicate("q", timeout=5)
        assert updates.install_update()
        install.assert_called_once()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows byte-range locks")
def test_runtime_and_installer_locks_exclude_each_other(tmp_path: Path):
    with guard.installation_lock(exclusive=False, root=tmp_path):
        with guard.installation_lock(exclusive=False, root=tmp_path):
            with pytest.raises(guard.UpdateBlockedError):
                with guard.installation_lock(exclusive=True, root=tmp_path):
                    pytest.fail("An installer entered a live environment")
    with guard.installation_lock(exclusive=True, root=tmp_path):
        with pytest.raises(guard.UpdateBlockedError):
            with guard.installation_lock(exclusive=False, root=tmp_path):
                pytest.fail("A runtime started while packages were being replaced")
        with pytest.raises(guard.UpdateBlockedError):
            with guard.installation_lock(exclusive=True, root=tmp_path):
                pytest.fail("Two installers entered the same environment")
    with guard.installation_lock(exclusive=True, root=tmp_path):
        pass
