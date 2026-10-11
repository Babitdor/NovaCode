"""Dangerous-command patterns must match the dangerous thing, not look-alikes.

The system-control case comes from a Terminal-Bench trajectory where
`pdflatex -halt-on-error` was blocked because `halt` appeared inside a flag.
"""

import re

import pytest

from novacode_cli.security.policy import _DEFAULT_SHELL_DENY
from novacode_cli.shell.utils import is_dangerous_command


def _policy_denies(command: str) -> bool:
    # The approval policy keeps its own copy of the rule; both must agree.
    return any(re.search(p, command) for p in _DEFAULT_SHELL_DENY)


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
    assert _policy_denies(command), command


@pytest.mark.parametrize(
    "command",
    [
        'rm -rf "/"',
        "rm -fr $HOME",
        "rm -rf ~/",
        "rm -rf /tmp/build /",  # the dangerous one is not the first argument
        "rm -rf /app/",
    ],
)
def test_the_dangerous_target_is_found_however_it_is_written(command):
    assert is_dangerous_command(command)[0], command
    assert _policy_denies(command), command


@pytest.mark.parametrize(
    "command",
    [
        # From a Terminal-Bench trial: plain cleanup, blocked by the old pattern.
        "rm -rf /tmp/povray-build",
        "rm -rf /app/build/ && make",
        "rm -rf /tmp/a /var/cache/apt/archives 2>/dev/null",
        "rm -rf build; cd /",
        "rm -rf '/tmp/my dir'",
    ],
)
def test_cleaning_up_below_the_top_level_is_allowed(command):
    assert not is_dangerous_command(command)[0], command
    assert not _policy_denies(command), command
