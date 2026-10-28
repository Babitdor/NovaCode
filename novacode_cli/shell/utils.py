"""Shell module: standalone utility functions.

This module contains the utility functions used by ShellMiddleware for
command validation, prompt detection, server readiness checks, and
environment sanitization.
"""

from __future__ import annotations

import re
import sys

from rich.text import Text

from novacode_cli.shell.patterns import (
    _API_KEY_SUFFIXES,
    _COMPILED_AUTO_ANSWER,
    _COMPILED_DANGEROUS,
    _COMPILED_PATTERNS,
    _COMPILED_SERVER_READY,
    _DANGEROUS_ENV_VARS,
    INTERACTIVE_COMMANDS,
    LONG_RUNNING_COMMANDS,
)


def _convert_unix_command_to_windows(command: str) -> str:
    """Convert Unix-style commands to Windows-compatible commands.

    This function transforms common Unix commands to work on Windows:
    - mkdir -p -> mkdir (Windows creates parent dirs automatically)
    - rm -rf -> rmdir /s /q or del /s /q
    - rm -r -> rmdir /s /q or del /s /q
    - cp -r -> xcopy /e /i /y
    - touch -> type nul >

    Args:
        command: The Unix-style command to convert

    Returns:
        Windows-compatible command string
    """
    if sys.platform != "win32":
        return command

    # Convert mkdir -p to Windows mkdir (Windows creates parent dirs automatically)
    command = re.sub(r"\bmkdir\s+-p\s+", "mkdir ", command, flags=re.IGNORECASE)

    # Convert rm -rf to Windows rmdir /s /q
    command = re.sub(r"\brm\s+-rf\s+", "rmdir /s /q ", command, flags=re.IGNORECASE)

    # Convert rm -r to Windows rmdir /s /q
    command = re.sub(r"\brm\s+-r\s+", "rmdir /s /q ", command, flags=re.IGNORECASE)

    # Convert cp -r to Windows xcopy
    command = re.sub(r"\bcp\s+-r\s+", "xcopy /e /i /y ", command, flags=re.IGNORECASE)

    # Convert touch to Windows type nul >
    return re.sub(r"\btouch\s+", "type nul > ", command, flags=re.IGNORECASE)


def _sanitize_env(env: dict[str, str]) -> dict[str, str]:
    """Strip dangerous and secret env vars from a subprocess environment copy.

    Removes all env vars whose names appear in ``_DANGEROUS_ENV_VARS`` OR
    that end with common API-key / token suffixes.

    Args:
        env: A mutable copy of working environment (e.g. ``os.environ.copy()``).

    Returns:
        The cleaned env dict.
    """
    to_strip: set[str] = set(_DANGEROUS_ENV_VARS)
    for key in env:
        upper = key.upper()
        for suffix in _API_KEY_SUFFIXES:
            if upper.endswith(suffix):
                to_strip.add(key)
                break
    for k in to_strip:
        env.pop(k, None)
    return env


def is_dangerous_command(command: str) -> tuple[bool, str]:
    """Return (True, pattern) if the command matches a destructive pattern.

    Args:
        command: The shell command string to inspect.

    Returns:
        A tuple of (is_dangerous, matched_pattern_string).
    """
    for pattern in _COMPILED_DANGEROUS:
        if pattern.search(command):
            return True, pattern.pattern
    return False, ""


def get_interactive_prompt(output: str) -> str | None:
    """Extract the current question/menu, excluding earlier questions in command logs."""
    # Cursor controls can be split across pipe chunks. Decode only the bounded
    # output tail, and tolerate log transports that dropped the ESC byte itself.
    normalized = output[-4096:].replace("\r\n", "\n").replace("\r", "\n")
    visible = Text.from_ansi(normalized).plain
    visible = re.sub(r"\[(?:\?[0-9;]*[hl]|[0-9;]*[KG])", "", visible)
    lines = [line for line in visible.replace("\r", "\n").splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if any(pattern.search(last) for pattern in _COMPILED_PATTERNS):
        return last.strip()
    # Selectors frequently end in an indented option, rather than a question.
    # Require an actual choice marker and a recent question/header so ordinary
    # indented compiler output does not itself become a prompt.
    recent = lines[-16:]
    choices = "●○◉◯\u276f\u2771\u203a"
    if not (last[0].isspace() or last.lstrip().startswith(tuple(choices))):
        return None
    if not any(marker in "\n".join(recent) for marker in choices):
        return None
    for index in range(len(recent) - 2, -1, -1):
        line = recent[index]
        if "?" in line or any(pattern.search(line) for pattern in _COMPILED_PATTERNS):
            return "\n".join(item.strip() for item in recent[index:])
    return None


def is_interactive_prompt(line: str) -> bool:
    """Detect if a line is an interactive prompt requiring user input.

    Args:
        line: The output line to check.

    Returns:
        True if the line appears to be a prompt requiring input.
    """
    return get_interactive_prompt(line) is not None


def get_auto_answer(prompt_text: str) -> str | None:
    """Return a safe automatic response for known package-manager prompts.

    Args:
        prompt_text: The prompt text to check.

    Returns:
        The auto-response string, or None if user input is required.
    """
    active = get_interactive_prompt(prompt_text)
    if active is None:
        return None
    # A selection UI cannot be driven by line-based stdin, even if its question
    # happens to contain the same words as a package-install confirmation.
    if any(marker in active for marker in "●○◉◯\u276f\u2771\u203a"):
        return None
    for pattern, response in _COMPILED_AUTO_ANSWER.items():
        if pattern.fullmatch(active):
            return response
    return None


def interactive_prompt_guidance(command: str) -> str:
    """Return recovery advice without modifying or replaying an approved command."""
    if re.search(r"\bcreate-astro\b|\b(?:npm|pnpm|yarn)\s+create\s+astro\b", command):
        return (
            "For create-astro, choose --git or --no-git explicitly and add --yes; "
            "keep your existing template, TypeScript and install options. "
            "Inspect the destination for files already created before retrying."
        )
    return (
        "Use the command's documented non-interactive flags or supply an explicit stdin answer. "
        "Arrow-key menus require an interactive terminal. "
        "Inspect any partial changes before retrying."
    )


def is_server_ready(line: str) -> bool:
    """Detect if a line indicates a server has successfully started.

    Args:
        line: The output line to check.

    Returns:
        True if the line indicates the server is ready.
    """
    stripped = line.strip()
    if not stripped:
        return False

    # Check against server ready patterns
    return any(pattern.search(stripped) for pattern in _COMPILED_SERVER_READY)


def is_long_running_command(command: str) -> bool:
    """Detect if a command is a known long-running server command.

    Args:
        command: The command to check.

    Returns:
        True if this is a known long-running command.
    """
    command_lower = command.lower()
    return any(pattern in command_lower for pattern in LONG_RUNNING_COMMANDS)


def is_interactive_command(command: str) -> bool:
    """Detect if a command is known to be interactive and requires user input.

    These commands typically prompt the user for configuration options,
    project names, framework selections, etc.

    Args:
        command: The command to check.

    Returns:
        True if this is a known interactive command.
    """
    command_lower = command.lower()
    return any(pattern in command_lower for pattern in INTERACTIVE_COMMANDS)


__all__ = [
    "_convert_unix_command_to_windows",
    "_sanitize_env",
    "get_auto_answer",
    "get_interactive_prompt",
    "interactive_prompt_guidance",
    "is_dangerous_command",
    "is_interactive_command",
    "is_interactive_prompt",
    "is_long_running_command",
    "is_server_ready",
]
