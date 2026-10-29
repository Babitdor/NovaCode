"""nova update's progress and finish screen: animated on a terminal, plain when piped."""

import io
import subprocess
from unittest.mock import Mock

import pytest

from novacode_cli import update_ui as display
from novacode_cli import updates
from novacode_cli.brand import wordmark


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("check_only", [True, False])
def test_update_accepts_legacy_windows_output_encoding(monkeypatch, check_only):
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(updates.sys, "stdout", output)
    monkeypatch.setattr(
        updates, "check_for_update", lambda **kwargs: updates.UpdateStatus(True, "old", "new")
    )
    monkeypatch.setattr(updates, "detect_installation", lambda: updates.Installation("1.0", "uv-tool"))
    install = Mock(return_value=True)
    monkeypatch.setattr(updates, "install_update", install)
    monkeypatch.setattr(updates, "installed_version", lambda python: "1.1")
    assert updates.update_main(["--check"] if check_only else []) == 0
    output.flush()
    rendered = raw.getvalue().decode("cp1252")
    assert ("Update available" if check_only else "NovaCode updated") in rendered
    assert install.call_count == (0 if check_only else 1)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(display.time, "sleep", lambda _s: None)
    monkeypatch.setattr(display.sys, "platform", "linux")


@pytest.mark.parametrize("interactive", [True, False])
def test_success_keeps_the_terminal_history_and_says_what_changed(interactive):
    output = Terminal() if interactive else io.StringIO()
    output.write("Installing packages…\n")
    display.show_success("2.4.5", "2.4.6", wordmark(), note="Faster updates", stream=output)
    rendered = output.getvalue()
    assert "\x1b[2J" not in rendered, "never clears the screen"
    assert "Installing packages" in rendered
    assert ("\x1b[38;2;" in rendered) is interactive, "the gradient logo only on a terminal"
    plain = rendered.replace("\x1b[1m", "").replace("\x1b[0m", "")
    assert "v2.4.5 → v2.4.6" in plain and "Faster updates" in plain
    assert plain.index("NovaCode updated") < plain.index("Run nova")


@pytest.mark.parametrize("interactive", [True, False])
def test_progress_marks_each_stage(interactive):
    output = Terminal() if interactive else io.StringIO()
    with display.Progress(output) as progress:
        progress.stage("Checking for updates")
        progress.line("resolving   packages")
        progress.done("a → b")
        progress.stage("Installing")
    rendered = output.getvalue()
    if interactive:
        assert "✓\x1b[0m Checking for updates" in rendered and rendered.endswith("\x1b[?25h")
    else:
        assert "done: Checking for updates (a → b)" in rendered and "\x1b" not in rendered
    assert "Installing" in rendered


def test_an_interrupted_stage_is_marked_failed():
    output = io.StringIO()
    with pytest.raises(KeyboardInterrupt), display.Progress(output) as progress:
        progress.stage("Installing")
        raise KeyboardInterrupt
    assert "failed: Installing" in output.getvalue()


def test_cli_success_reads_updated_version_after_install(monkeypatch, capsys):
    installed = False
    monkeypatch.setattr(
        updates, "check_for_update", lambda **kwargs: updates.UpdateStatus(True, "old", "new")
    )
    monkeypatch.setattr(updates, "detect_installation", lambda: updates.Installation("9.8.6", "uv-tool"))

    def install(progress):
        nonlocal installed
        installed = True
        return True

    def version(python):
        assert installed
        return "9.8.7"

    monkeypatch.setattr(updates, "install_update", install)
    monkeypatch.setattr(updates, "installed_version", version)
    assert updates.update_main([]) == 0
    assert "v9.8.6 → v9.8.7" in capsys.readouterr().out


def test_failed_update_keeps_progress_and_does_not_show_success(monkeypatch, capsys):
    monkeypatch.setattr(
        updates, "check_for_update", lambda **kwargs: updates.UpdateStatus(True, "old", "new")
    )

    def install(progress):
        print("Installer diagnostics")
        raise subprocess.CalledProcessError(1, "installer")

    monkeypatch.setattr(updates, "install_update", install)
    assert updates.update_main([]) == 1
    captured = capsys.readouterr()
    assert "Installer diagnostics" in captured.out
    assert "Update failed" in captured.err
    assert "NovaCode updated" not in captured.out


def test_installed_version_uses_target_interpreter(monkeypatch):
    read = Mock(return_value="3.2.1\n")
    monkeypatch.setattr(display.subprocess, "check_output", read)
    assert display.installed_version("updated-python.exe") == "3.2.1"
    assert read.call_args.args[0][0] == "updated-python.exe"


def test_version_fallback_reads_new_file_without_importing_old_version(monkeypatch, tmp_path):
    monkeypatch.setattr(display.subprocess, "check_output", Mock(side_effect=OSError("missing")))
    monkeypatch.setattr(display, "__file__", str(tmp_path / "update_ui.py"))
    (tmp_path / "_version.py").write_text('__version__ = "7.8.9"\n', encoding="utf-8")
    assert display.installed_version("gone-python.exe") == "7.8.9"
