"""Exercise Windows executable replacement without changing an installed tool."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from novacode_cli import updates as U  # noqa: N812


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable locking")
def test_running_executable_can_be_renamed_but_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe"
    launcher = tmp_path / "nova.exe"
    shutil.copyfile(command, launcher)
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nentrypoints = [{name = "nova", from = "novacode-cli", install-path = '
        + json.dumps(str(launcher))
        + "}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(U.sys, "prefix", str(tmp_path))
    process = subprocess.Popen(  # noqa: S603
        [str(launcher), "/c", "pause"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.read(1)
        assert process.poll() is None
        with pytest.raises(PermissionError):
            shutil.copyfile(command, launcher)
        with U._windows_uv_entrypoint():
            assert not launcher.exists()
            backups = list(tmp_path.glob(".nova-update-*.exe"))
            assert len(backups) == 1
            shutil.copyfile(command, launcher)
        assert launcher.is_file()
        assert process.poll() is None
    finally:
        process.communicate(b"\n", timeout=5)
