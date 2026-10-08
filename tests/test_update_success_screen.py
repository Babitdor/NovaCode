"""Successful updates clear interactive progress and show the installed release."""

import io
import subprocess
from unittest.mock import Mock

import pytest

from novacode_cli import _windows_update as display
from novacode_cli import updates
from novacode_cli.brand import wordmark


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("interactive", [True, False])
def test_success_screen_clears_only_interactive_output(monkeypatch, interactive):
    output = Terminal() if interactive else io.StringIO()
    monkeypatch.setattr(display.sys, "stdout", output)
    monkeypatch.setattr(display.sys, "platform", "linux")
    print("Installing packages…")
    display.show_update_success("2.4.6", wordmark())
    rendered = output.getvalue()
    if interactive:
        assert "\x1b[2J\x1b[H" in rendered
        rendered = rendered.split("\x1b[2J\x1b[H", 1)[1]
        assert "Installing packages" not in rendered
    else:
        assert "\x1b" not in rendered
        assert "Installing packages" in rendered
    assert "███" in rendered
    assert "v2.4.6" in rendered
    assert "Update successful." in rendered
    assert rendered.index("Update successful.") < rendered.index("Restart Nova")


def test_cli_success_reads_updated_version_after_install(monkeypatch, capsys):
    installed = False
    monkeypatch.setattr(
        updates, "check_for_update", lambda **kwargs: updates.UpdateStatus(True, "old", "new")
    )

    def install():
        nonlocal installed
        installed = True
        print("Installer progress")
        return False

    def version(python):
        assert installed
        return "9.8.7"

    monkeypatch.setattr(updates, "install_update", install)
    monkeypatch.setattr(display, "installed_version", version)
    assert updates.update_main([]) == 0
    assert "v9.8.7" in capsys.readouterr().out


def test_failed_update_keeps_progress_and_does_not_show_success(monkeypatch, capsys):
    monkeypatch.setattr(
        updates, "check_for_update", lambda **kwargs: updates.UpdateStatus(True, "old", "new")
    )

    def install():
        print("Installer diagnostics")
        raise subprocess.CalledProcessError(1, "installer")

    monkeypatch.setattr(updates, "install_update", install)
    assert updates.update_main([]) == 1
    captured = capsys.readouterr()
    assert "Installer diagnostics" in captured.out
    assert "Update failed" in captured.err
    assert "Update successful" not in captured.out and "\x1b[2J" not in captured.out


def test_installed_version_uses_target_interpreter(monkeypatch):
    read = Mock(return_value="3.2.1\n")
    monkeypatch.setattr(display.subprocess, "check_output", read)
    assert display.installed_version("updated-python.exe") == "3.2.1"
    assert read.call_args.args[0][0] == "updated-python.exe"


def test_version_fallback_reads_new_file_without_importing_old_version(monkeypatch, tmp_path):
    monkeypatch.setattr(display.subprocess, "check_output", Mock(side_effect=OSError("missing")))
    monkeypatch.setattr(display, "__file__", str(tmp_path / "_windows_update.py"))
    (tmp_path / "_version.py").write_text('__version__ = "7.8.9"\n', encoding="utf-8")
    assert display.installed_version("gone-python.exe") == "7.8.9"


def test_windows_handoff_passes_logo_and_version_interpreter(monkeypatch):
    import json

    process = Mock()
    monkeypatch.setattr(updates.subprocess, "Popen", process)
    updates._start_windows_update(["uv", "tool", "upgrade", "novacode-cli"])
    payload = json.loads(process.call_args.args[0][-1])
    assert payload["logo"] == wordmark()
    assert payload["version_python"] == updates.sys.executable
