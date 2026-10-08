"""Windows update waits for an unrenameable executable to exit before replacing it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from novacode_cli import _windows_update as helper


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable locking")
@pytest.mark.parametrize("wait_for_parent", [False, True])
def test_updater_waits_for_locked_launcher_then_replaces_it(
    tmp_path: Path,
    *,
    wait_for_parent: bool,
) -> None:
    command = Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe"
    launcher = tmp_path / "nova.exe"
    shutil.copyfile(command, launcher)
    process = subprocess.Popen(  # noqa: S603
        [str(launcher), "/c", "pause"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    updater = None
    try:
        assert process.stdout is not None
        assert process.stdout.read(1)
        with launcher.open("rb"):
            with pytest.raises(PermissionError):
                launcher.rename(tmp_path / "backup.exe")
            payload = {
                "caller_pid": 0 if wait_for_parent else process.pid,
                "parent_pid": process.pid if wait_for_parent else os.getpid(),
                "command": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'new')",
                    str(launcher),
                ],
                "cache": str(tmp_path / "cache.json"),
            }
            updater = subprocess.Popen(  # noqa: S603
                [sys.executable, "-I", helper.__file__, json.dumps(payload)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert updater.stdout is not None
            assert updater.stdout.readline().startswith(b"Waiting")
            time.sleep(0.3)
            assert updater.poll() is None
            assert launcher.read_bytes() != b"new"
        process.communicate(b"\n", timeout=5)
        output, error = updater.communicate(timeout=10)
        assert updater.returncode == 0, error
        assert b"Update successful." in output
        assert launcher.read_bytes() == b"new"
    finally:
        if process.poll() is None:
            process.communicate(b"\n", timeout=5)
        if updater is not None and updater.poll() is None:
            updater.kill()
            updater.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater handoff")
@pytest.mark.parametrize(
    ("installer_output", "exit_code", "result"),
    [
        ("Updated novacode-cli", 0, "Update successful."),
        ("Nothing to upgrade", 0, "NovaCode is up to date."),
        ("Installation failed", 1, "Update failed:"),
    ],
)
def test_windows_handoff_streams_progress_and_result_without_log(
    tmp_path: Path, installer_output: str, exit_code: int, result: str
) -> None:
    release = tmp_path / "finish-installing"
    installer = [
        sys.executable,
        "-u",
        "-c",
        "import sys, time; from pathlib import Path; "
        "print('Installing Nova...', flush=True); "
        "\nwhile not Path(sys.argv[1]).exists(): time.sleep(0.01)"
        "\nprint(sys.argv[2], flush=True); sys.exit(int(sys.argv[3]))",
        str(release),
        installer_output,
        str(exit_code),
    ]
    caller = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import json, sys; from pathlib import Path; from novacode_cli import updates; "
            "sys._base_executable = sys.executable; "
            "updates._cache_path = lambda: Path(sys.argv[2]); "
            "updates._start_windows_update(json.loads(sys.argv[1]))",
            json.dumps(installer),
            str(tmp_path / "cache.json"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert caller.stdout is not None
        assert caller.stdout.readline().startswith(b"Waiting")
        # Progress is observable before allowing the installer to finish.
        assert caller.stdout.readline().strip() == b"Installing Nova..."
        assert caller.wait(timeout=5) == 0
        assert not release.exists()
    finally:
        release.touch()
        output, _error = caller.communicate(timeout=10)
    assert result.encode() in output
    assert not list(tmp_path.glob("*.log"))
