"""The shell must wait for the whole update chain without loading agent DLLs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from novacode_cli import windows_launcher as launcher


@pytest.fixture(autouse=True)
def isolated_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (launcher.CHILD_ENV, launcher.OWNER_FILE_ENV, "NOVA_UPDATE_HANDOFF_PID"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("arguments", [[], ["--continue", "saved"]])
def test_shell_owner_waits_across_multiple_updates(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    events = []
    control = None

    def spawn(command: list[str], **kwargs: object) -> Mock:
        nonlocal control
        events.append("runtime")
        assert command == [sys.executable, "-P", "-m", "novacode_cli", *arguments]
        assert kwargs["creationflags"] == 0
        env = kwargs["env"]
        assert env[launcher.CHILD_ENV] == "1"
        control = Path(env[launcher.OWNER_FILE_ENV])

        def exit_runtime() -> int:
            control.write_text("101", encoding="ascii")
            return 0

        return Mock(wait=Mock(side_effect=[KeyboardInterrupt, exit_runtime()]))

    def wait_for_updater(pid: int) -> int:
        events.append(pid)
        if pid == 101:
            control.write_text("202", encoding="ascii")
            return 0
        return 7

    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "_wait_for_process", wait_for_updater)
    assert launcher.supervise(arguments) == 7
    assert events == ["runtime", 101, 202]
    assert control is not None
    assert not control.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["-p", "hello"],
        ["-phello"],
        ["--print", "hello"],
        ["--print=hello"],
        ["--session-worker"],
        ["--headless"],
        ["--mode", "pipe"],
        ["--mode=pipe"],
    ],
)
def test_pipe_and_one_shot_commands_are_not_wrapped(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.supervise(arguments) is None
    spawn.assert_not_called()


def test_runtime_marker_is_consumed_and_prevents_recursive_launchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setenv(launcher.CHILD_ENV, "1")
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.supervise([]) is None
    assert launcher.CHILD_ENV not in os.environ
    spawn.assert_not_called()


def test_legacy_restart_gets_an_interactive_console_without_the_resumed_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setenv("NOVA_UPDATE_HANDOFF_PID", "123")
    spawn = Mock(return_value=Mock(wait=Mock(return_value=0)))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.supervise(["--continue", "saved"]) == 0
    assert spawn.call_args.kwargs["creationflags"] == 0x10


def test_updater_publishes_only_to_its_inherited_owner_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    control = tmp_path / "owner.pid"
    monkeypatch.setenv(launcher.OWNER_FILE_ENV, str(control))
    launcher.publish_handoff(234)
    assert control.read_text(encoding="ascii") == "234"


def test_native_wait_keeps_handle_open_until_exit_and_propagates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    kernel = Mock()
    kernel.OpenProcess.return_value = 123
    kernel.WaitForSingleObject.side_effect = [258, KeyboardInterrupt, 0]

    def exit_code(_handle: int, code: object) -> int:
        code._obj.value = 19
        return 1

    kernel.GetExitCodeProcess.side_effect = exit_code
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    assert launcher._wait_for_process(234) == 19
    kernel.CloseHandle.assert_called_once_with(123)


def test_posix_does_not_gain_a_windows_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.supervise([]) is None
    spawn.assert_not_called()


def test_shell_owner_never_imports_agent_or_third_party_native_dependencies() -> None:
    code = """
import sys
from types import SimpleNamespace
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('novacode_cli.main', 'requests', 'rich', 'textual',
                                'langchain', 'deepagents', 'numpy')):
            raise AssertionError('launcher loaded runtime dependency: ' + fullname)
sys.meta_path.insert(0, Guard())
from novacode_cli import windows_launcher
windows_launcher.sys.platform = 'win32'
windows_launcher.subprocess.Popen = lambda *a, **kw: SimpleNamespace(wait=lambda: 0)
from novacode_cli.entrypoint import cli_main
sys.argv = ['nova']
cli_main()
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and test program, no shell
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
