"""CLI prompts must not hang or inherit answers from older installer output."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from novacode_cli.shell.middleware import ShellMiddleware
from novacode_cli.shell.utils import get_auto_answer, is_interactive_command, is_interactive_prompt

if TYPE_CHECKING:
    from pathlib import Path

ASTRO_PROMPT = (
    "\x1b[?25l\x1b[2K\x1b[1G\n"
    "   git   Initialize a new git repository? (optional)\n"
    "         ● Yes  ○ No "
)


@pytest.mark.parametrize(
    "command",
    [
        "npx create-astro@5.3.1 portfolio --template minimal",
        "npx --yes create-astro@5.3.1 portfolio",
        "npm create astro@latest portfolio -- --template minimal",
        "pnpm create astro portfolio",
        "yarn create astro portfolio",
    ],
)
def test_astro_creators_are_recognized(command: str):
    assert is_interactive_command(command)


@pytest.mark.parametrize(
    "prompt",
    [
        ASTRO_PROMPT,
        "[?25l[2K[1G\nInitialize a new git repository? (optional)\n● Yes ○ No",
        "Select a framework:\n\u276f React\n  Vue\n  Svelte",
        "Which template would you like?\n\u276f Minimal\n  Blog",
    ],
)
def test_modern_selection_prompts_are_detected(prompt: str):
    assert is_interactive_prompt(prompt)
    assert get_auto_answer(prompt) is None


def test_old_install_confirmation_cannot_answer_new_git_question():
    output = "Need to install packages. Ok to proceed? (y)\nInstalling...\n" + ASTRO_PROMPT
    assert get_auto_answer(output) is None


def test_completed_prompt_in_old_logs_does_not_abort_a_running_build():
    output = "Ok to proceed? (y)\nInstalling...\nCompiling application"
    assert not is_interactive_prompt(output)
    assert get_auto_answer(output) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "Do you want to install packages and overwrite existing files? (y/n)",
        "Ok to proceed with deleting the project? (y/n)",
        "Do you want to install and publish the package? (y/n)",
    ],
)
def test_install_words_do_not_approve_destructive_or_publish_choices(prompt: str):
    assert get_auto_answer(prompt) is None


@pytest.mark.timeout(20)
def test_selection_marker_split_across_utf8_reads_is_detected(tmp_path: Path):
    chunks = [b"Which template?\n", b"\xe2", b"\x9d", b"\xaf", b" Minimal\n  Blog "]
    code = (
        "import sys,time\n"
        f"for chunk in {chunks!r}:\n"
        " sys.stdout.buffer.write(chunk); sys.stdout.flush(); time.sleep(.06)\n"
        "sys.stdin.readline()\n"
    )
    result = ShellMiddleware(workspace_root=str(tmp_path), timeout=4)._run_local_command(
        code,
        tool_call_id="split",
        prog=[sys.executable, "-c"],
    )
    assert result.status == "error"
    assert "waiting for interactive input" in result.content
    assert "exceeded" not in result.content


@pytest.mark.timeout(30)
@pytest.mark.parametrize("delay", [0.0, 5.2])
def test_selection_prompt_stops_without_answering_or_replaying(tmp_path: Path, delay: float):
    marker = tmp_path / "answer.txt"
    output = "Ok to proceed? (y)\nInstalled packages\n" + ASTRO_PROMPT
    code = (
        "import sys,time\n"
        f"time.sleep({delay})\n"
        f"sys.stdout.buffer.write({output.encode()!r}); sys.stdout.flush()\n"
        "answer=sys.stdin.readline()\n"
        f"open({str(marker)!r}, 'w').write(answer)\n"
    )
    result = ShellMiddleware(workspace_root=str(tmp_path), timeout=9)._run_local_command(
        code,
        tool_call_id="selection",
        prog=[sys.executable, "-c"],
    )
    assert result.status == "error", result.content
    assert "waiting for interactive input" in result.content
    assert "exceeded" not in result.content
    assert not marker.exists(), "Nova must not send the earlier npm answer to a new Git question"
    assert result.artifact["nova_execution"]["completed"] is False


@pytest.mark.timeout(20)
def test_known_package_confirmation_still_works(tmp_path: Path):
    code = (
        "import sys\n"
        "print('Ok to proceed? (y)', flush=True)\n"
        "print('answer=' + sys.stdin.readline().strip(), flush=True)\n"
    )
    result = ShellMiddleware(workspace_root=str(tmp_path), timeout=5)._run_local_command(
        code,
        tool_call_id="install",
        prog=[sys.executable, "-c"],
    )
    assert result.status == "success", result.content
    assert "answer=y" in result.content


def test_blocked_prompt_is_not_a_reason_to_retry_in_another_shell():
    from langchain_core.messages import ToolMessage

    from novacode_cli.shell.middleware import _looks_like_shell_rejection

    result = ToolMessage(
        content="Optional helper: command not found\nWaiting for interactive input",
        tool_call_id="blocked",
        status="error",
        artifact={"interactive_prompt": {"text": "Initialize Git?", "input_required": True}},
    )
    assert not _looks_like_shell_rejection(result)


@pytest.mark.timeout(25)
def test_repeated_package_confirmations_are_bounded(tmp_path: Path):
    marker = tmp_path / "too_many_answers"
    code = (
        "import sys\n"
        "for index in range(9):\n"
        " print('Ok to proceed? (y)', flush=True)\n"
        " sys.stdin.readline()\n"
        f"open({str(marker)!r}, 'w').write('unexpected ninth answer')\n"
    )
    result = ShellMiddleware(workspace_root=str(tmp_path), timeout=9)._run_local_command(
        code,
        tool_call_id="repeat",
        prog=[sys.executable, "-c"],
    )
    assert result.status == "error", result.content
    assert "confirmation limit" in result.content
    assert not marker.exists()


@pytest.mark.timeout(20)
def test_real_install_confirmation_then_selection_does_not_reuse_answer(tmp_path: Path):
    marker = tmp_path / "selected_git"
    code = (
        "import sys\n"
        "print('Ok to proceed? (y)', flush=True)\n"
        "print('accepted:' + sys.stdin.readline().strip(), flush=True)\n"
        f"sys.stdout.buffer.write({ASTRO_PROMPT.encode()!r}); sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
        f"open({str(marker)!r}, 'w').write('unexpected Git selection')\n"
    )
    result = ShellMiddleware(workspace_root=str(tmp_path), timeout=6)._run_local_command(
        code,
        tool_call_id="two-prompts",
        prog=[sys.executable, "-c"],
    )
    assert "accepted:y" in result.content
    assert result.status == "error"
    assert not marker.exists()
    prompt = result.artifact["interactive_prompt"]["text"]
    assert "Initialize" in prompt
    assert "Ok to proceed" not in prompt
    assert "\x1b" not in prompt
