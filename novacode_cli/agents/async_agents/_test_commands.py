"""The background test runner's one tool: test-runner commands only, no shell.

Kept apart from ``test_runner.py`` because that module builds a graph (and a
model) on import, and this is the part that has to be testable on its own.
"""

from __future__ import annotations

import shlex
import subprocess

from langchain_core.tools import tool

from novacode_cli.agents.async_workspace import workspace_root

#: What a command may start with. This runs unattended, so it is a list of test
#: runners rather than a shell: nothing here can be told to delete or push.
ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python", "-m", "unittest"),
    ("uv", "run", "pytest"),
    ("uv", "run", "python", "-m", "pytest"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("npx", "vitest"),
    ("npx", "jest"),
    ("go", "test"),
    ("cargo", "test"),
    ("dotnet", "test"),
    ("mvn", "test"),
    ("gradle", "test"),
    ("make", "test"),
)
_SHELL_CHARS = set("|&;<>`$\n\r")
_TAIL_CHARS = 12_000


def parse_test_command(command: str) -> list[str] | str:
    """The argv for *command*, or a message saying why it will not be run."""
    if _SHELL_CHARS & set(command):
        return "Refused: pipes, redirects and chained commands are not allowed. Give one test command."
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return f"Refused: could not parse the command ({exc})."
    head = [a.lower().removesuffix(".exe") for a in argv]
    if head and head[0] in ("python3", "py"):
        head[0] = "python"
    if not any(tuple(head[: len(p)]) == p for p in ALLOWED_PREFIXES):
        allowed = ", ".join(" ".join(p) for p in ALLOWED_PREFIXES)
        return f"Refused: only test runners can be run here. Start the command with one of: {allowed}."
    return argv


@tool
def run_tests(command: str, timeout_seconds: int = 900) -> str:
    """Run a test command in the project and return its exit code and output.

    Args:
        command: One test-runner command, e.g. ``pytest tests/ -x -q`` or ``npm test``.
            No pipes, redirects or chaining.
        timeout_seconds: Give up after this long (default 15 minutes, at most 60).

    Returns:
        The exit code and the end of the combined output.
    """
    argv = parse_test_command(command)
    if isinstance(argv, str):
        return argv
    try:
        result = subprocess.run(  # noqa: S603 — argv is allowlisted above, no shell
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            cwd=workspace_root(),
            timeout=max(10, min(int(timeout_seconds), 3600)),
        )
    except FileNotFoundError:
        return f"Could not run `{argv[0]}`: it is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout_seconds}s. Run a smaller selection of tests."
    output = (result.stdout or "") + (result.stderr or "")
    if len(output) > _TAIL_CHARS:
        output = "… (earlier output omitted)\n" + output[-_TAIL_CHARS:]
    return f"exit code {result.returncode}\n{output}"
