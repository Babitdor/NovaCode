"""Exercise the Windows console driver across a real three-process update chain."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


def _wait_for(predicate: Callable[[], bool], output: list[str], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "".join(output)[-5000:]
        time.sleep(0.05)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console ownership regression")
@pytest.mark.timeout(60)  # Two five-second TUI lifetimes plus real shell/interpreter startup.
def test_update_resume_keeps_shell_blocked_and_accepts_typing_and_scroll(tmp_path: Path) -> None:
    from winpty import PtyProcess

    from novacode_cli.windows_launcher import CHILD_ENV, OWNER_FILE_ENV

    fixture = Path(__file__).parent / "fixtures" / "update_terminal_child.py"

    def quote(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    marker = tmp_path / "shell-returned"
    command = (
        f"& {quote(sys.executable)} {quote(fixture)} launcher {quote(tmp_path)}; "
        f"Set-Content -LiteralPath {quote(marker)} -Value $LASTEXITCODE"
    )
    env = dict(os.environ)
    for key in (CHILD_ENV, OWNER_FILE_ENV, "NOVA_UPDATE_HANDOFF_PID"):
        env.pop(key, None)
    terminal = PtyProcess.spawn(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit", "-Command", command],
        env=env,
        cwd=str(tmp_path),
        dimensions=(30, 100),
    )
    output = []

    def drain() -> None:
        try:
            while True:
                output.append(terminal.read(4096))
        except (EOFError, OSError):
            pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    try:
        ready = tmp_path / "ready"
        _wait_for(ready.exists, output)
        assert not marker.exists(), "PowerShell resumed before the relaunched TUI exited"
        terminal.write("composer-input")
        terminal.write("\x1b[<65;10;10M" * 4)

        def interactive() -> bool:
            try:
                state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False  # a Windows rename may briefly exclude readers
            return state["text"] == "composer-input" and state["scroll"] > 0

        _wait_for(interactive, output, timeout=4)
        assert not marker.exists()
        second_ready = tmp_path / "ready-second"
        _wait_for(second_ready.exists, output)
        assert not marker.exists(), "A second update lost the original terminal owner"
        terminal.write("composer-input")
        terminal.write("\x1b[<65;10;10M" * 4)
        _wait_for(interactive, output, timeout=4)
        assert not marker.exists()

        def clean_shell_return() -> bool:
            try:
                code = marker.read_text(encoding="utf-8-sig").strip()
            except OSError:
                return False
            assert code in {"", "0"}, f"Launcher exited with {code}"
            return code == "0"

        _wait_for(clean_shell_return, output)
    finally:
        terminal.close(force=True)
        reader.join(timeout=2)
