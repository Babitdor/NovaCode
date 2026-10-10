"""Dangerous-command patterns must match the dangerous thing, not look-alikes.

The system-control case comes from a Terminal-Bench trajectory where
`pdflatex -halt-on-error` was blocked because `halt` appeared inside a flag.
"""

import pytest

from novacode_cli.shell.utils import is_dangerous_command


@pytest.mark.parametrize(
    "command",
    [
        "shutdown -h now",
        "sudo reboot",
        "make && halt",
        "echo done; poweroff",
        "systemctl reboot",
        "cd /tmp\nhalt",
    ],
)
def test_system_control_commands_are_blocked(command):
    assert is_dangerous_command(command)[0]


@pytest.mark.parametrize(
    "command",
    [
        "pdflatex -interaction=nonstopmode -halt-on-error main.tex",
        "grep -rn 'reboot' docs/",
        "cat shutdown.log",
        "python halt_detector.py --poweroff-threshold 3",
    ],
)
def test_words_containing_the_command_are_allowed(command):
    assert not is_dangerous_command(command)[0]


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf /etc",
        # The grader's own directory: an agent really did try this, and the
        # guard stopping it is exactly what should happen.
        "rm -rf /tests && cat /app/out.html",
    ],
)
def test_wiping_the_root_home_or_a_top_level_directory_is_blocked(command):
    assert is_dangerous_command(command)[0], command
