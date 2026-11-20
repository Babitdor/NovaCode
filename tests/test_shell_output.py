"""Shell results keep the end of long output and report exit status honestly."""

from __future__ import annotations

import signal

import pytest

from novacode_cli.shell import output
from novacode_cli.shell.output import OutputWindow, exit_status


def test_long_output_keeps_head_and_final_error_and_logs_everything():
    window = OutputWindow(200)
    window.add("$ pytest\n")
    for i in range(500):
        window.add(f"collected line {i}\n")
    window.add("FAILED tests/test_x.py::test_y - AssertionError\n1 failed\n")
    text = window.text()
    assert text.startswith("$ pytest\n")
    assert text.endswith("FAILED tests/test_x.py::test_y - AssertionError\n1 failed\n")
    assert "characters omitted from the middle" in text
    assert window.log_path is not None
    full = window.log_path.read_text(encoding="utf-8")
    assert "collected line 250\n" in full
    assert full.startswith("$ pytest\ncollected line 0\n")
    assert full.endswith("1 failed\n")


def test_short_output_is_unchanged_and_not_logged():
    window = OutputWindow(200)
    window.add("a" * 150)
    window.add("b" * 40)
    assert window.text() == "a" * 150 + "b" * 40
    assert window.log_path is None


def test_logs_are_pruned():
    for _ in range(output.MAX_SHELL_LOGS + 5):
        opened = output.open_shell_log()
        assert opened is not None
        opened[0].close()
    assert len(list(output.shell_log_dir().glob("*.log"))) == output.MAX_SHELL_LOGS


@pytest.mark.parametrize(
    ("command", "code", "note", "failed"),
    [
        ("pytest", 0, "", False),
        ("pytest -q", 2, "Exit code: 2", True),
        ('rg "TODO" src', 1, "No matches found (rg exit code 1).", False),
        ("git log | grep fix", 1, "No matches found (grep exit code 1).", False),
        ("diff a.txt b.txt", 1, "Files differ (diff exit code 1).", False),
        ("grep x f && make", 1, "Exit code: 1", True),
        ("rg x f", 2, "Exit code: 2", True),
        ("LC_ALL=C grep -q x f", 1, "No matches found (grep exit code 1).", False),
    ],
)
def test_exit_status(command: str, code: int, note: str, failed: bool):  # noqa: FBT001
    assert exit_status(command, code) == (note, failed)


def test_signal_deaths_use_128_plus_n():
    assert exit_status("sleep 100", -signal.SIGTERM) == ("Exit code: 143 (killed by SIGTERM)", True)


# --- terminal colour -------------------------------------------------------
# PowerShell's Select-String highlights the match inside the line it prints, so
# a search result reached the model as `config.py ESC[0m ESC[7m 38%` — the path
# it needed to act on, split by escapes.

_COLOURED = "\x1b[7mconfig\x1b[0m.py \x1b[31;1m38%\x1b[0m"


def test_strip_ansi_removes_colour_and_osc():
    assert output.strip_ansi(_COLOURED) == "config.py 38%"
    assert output.strip_ansi("\x1b]0;title\x07text") == "text"
    assert output.strip_ansi("\x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\") == "link"


def test_strip_ansi_leaves_plain_text_alone():
    plain = "plain text, 100% complete, a > b"
    assert output.strip_ansi(plain) is plain


def test_window_text_drops_colour():
    window = OutputWindow(200)
    window.add(_COLOURED + "\n")
    assert window.text() == "config.py 38%\n"


def test_window_text_drops_colour_around_the_omission_note():
    window = OutputWindow(200)
    window.add("\x1b[7m" + "a" * 200 + "\x1b[0m\n")
    window.add("\x1b[32mok\x1b[0m\n")
    text = window.text()
    assert "\x1b" not in text
    assert text.endswith("ok\n")


# --- commands that only wait ------------------------------------------------
# A stuck polling loop looks like `Start-Sleep -Seconds 29` once per model round
# trip; the advice is said once per session, not on every sleep.


@pytest.mark.parametrize(
    "command",
    ["Start-Sleep -Seconds 29", "start-sleep -s 5", "sleep 30", "Start-Sleep 12;", "  Sleep 3  "],
)
def test_wait_only_commands_are_recognised(command: str):
    assert output.is_wait_only_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "Start-Sleep -Seconds 5; gh run view 1",
        "pytest -q",
        "sleep 30 && pytest",
        "Get-ChildItem .",
        "",
        "sleep",
    ],
)
def test_work_with_a_wait_in_it_is_not_a_wait(command: str):
    assert output.is_wait_only_command(command) is False


def test_the_wait_note_points_at_the_resume_path():
    assert "end your turn" in output.WAIT_ONLY_NOTE
    assert "get_task_logs" not in output.WAIT_ONLY_NOTE
