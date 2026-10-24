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
