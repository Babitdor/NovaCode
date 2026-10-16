"""/codex-plan: OpenAI Codex plans headless; Nova approves and implements.

Each turn runs ``codex exec --json`` in the project with a read-only sandbox
(Codex has no plan mode; read-only is what keeps it to planning) and reads its
JSONL events. Codex cannot write a plan file read-only, so the plan is its final
message, ending in ``PLAN_READY``; questions are a final message without it.
Answers and refinements resume the same thread with ``codex exec resume``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from novacode_cli.commands.claude_plan import (
    PLAN_READY,
    QUESTIONS_PROTOCOL,
    PlanTurn,
    cli_env,
    needs_login,
    parse_questions,
)

NAME = "Codex"
COMMAND = "/codex-plan"
DOCS_URL = "https://github.com/openai/codex"
COLOR = "#10a37f"
INSTALL_LABEL = "npm install -g @openai/codex"
LOGIN_HINT = (
    "Log in once: run `codex login` in a terminal (or set OPENAI_API_KEY), "
    "then run /codex-plan again."
)

INSTRUCTIONS = f"""\
You are planning for another coding agent (Nova), which will implement your plan.
Do not modify any files: explore the repository read-only as much as you need.
The user answers only between your turns.
{QUESTIONS_PROTOCOL}
- When the plan is complete, reply with the full plan in markdown: context, the
  concrete steps with the files to change, and how to verify. End the reply
  with a line containing only {PLAN_READY}.

Task:
"""


def executable() -> str | None:
    """The ``codex`` CLI, if installed."""
    return shutil.which("codex")


def install_command() -> list[str]:
    """Codex's npm package (needs Node.js)."""
    npm = shutil.which("npm") or "npm"
    return [npm, "install", "-g", "@openai/codex"]


async def install(on_line: Callable[[str], None]) -> bool:
    """Install with npm, streaming its output; True when ``codex`` is then found."""
    if shutil.which("npm") is None:
        on_line("npm is not installed: install Node.js (https://nodejs.org), then retry.")
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            *install_command(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        on_line(f"Could not start the installer: {exc}")
        return False
    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.decode(errors="replace").rstrip()
        if line:
            on_line(line)
    await process.wait()
    return executable() is not None


_SHELL_WRAPPER = re.compile(r'^"?[^"]*?(?:pwsh|powershell|bash|sh)(?:\.exe)?"?\s+(?:-NoProfile\s+)?-(?:Command|c|lc)\s+', re.I)


def _brief(command: str) -> str:
    """``"C:\\...\\pwsh.exe" -Command 'Get-Content app.py'`` -> ``$ Get-Content app.py``."""
    inner = _SHELL_WRAPPER.sub("", command).strip().strip("'\"")
    return "$ " + " ".join(inner.split())[:90]


#: Nova's exported OpenAI settings (see ``claude_plan.ANTHROPIC_VARS``): a
#: custom base URL from Nova's model settings would send Codex elsewhere.
OPENAI_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY")


async def run_turn(
    prompt: str,
    cwd: Path,
    *,
    session_id: str | None = None,
    on_step: Callable[[str], None] | None = None,
) -> PlanTurn:
    """Run one headless planning turn; ``on_step`` receives each command Codex runs.

    Runs on the user's own ``codex login``. Only when there is none, and Nova
    has an OpenAI key, is the turn retried with that key.
    """
    env, has_key = cli_env(OPENAI_VARS)
    turn = await _run(prompt, cwd, session_id=session_id, on_step=on_step, env=env)
    if turn.error and has_key and needs_login(turn.error):
        turn = await _run(
            prompt, cwd, session_id=session_id, on_step=on_step,
            env=cli_env(OPENAI_VARS, keep=True)[0],
        )
    if turn.error and needs_login(turn.error):
        turn.error = f"Codex is not logged in. {LOGIN_HINT}\n({turn.error[:200]})"
    return turn


async def _run(
    prompt: str,
    cwd: Path,
    *,
    session_id: str | None,
    on_step: Callable[[str], None] | None,
    env: dict[str, str],
) -> PlanTurn:
    exe = executable()
    if exe is None:
        return PlanTurn(error="Codex is not installed (no `codex` found).")
    common = ["--json", "--skip-git-repo-check", "-c", 'sandbox_mode="read-only"']
    if session_id:
        command = [exe, "exec", "resume", session_id, *common, prompt]
    else:
        command = [exe, "exec", *common, INSTRUCTIONS + prompt]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # else it waits for piped input
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )
    turn = PlanTurn(session_id=session_id)
    try:
        assert process.stdout is not None
        async for raw in process.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "thread.started":
                turn.session_id = event.get("thread_id") or turn.session_id
            elif kind == "item.started" and item.get("type") == "command_execution":
                if on_step is not None:
                    on_step(_brief(str(item.get("command") or "")))
            elif kind == "item.completed" and item.get("type") == "agent_message":
                turn.text = str(item.get("text") or "")  # the last one is the reply
            elif kind in ("turn.failed", "error"):
                error = event.get("error") or {}
                turn.error = str(error.get("message") if isinstance(error, dict) else error) or str(
                    event.get("message") or "Codex failed"
                )
        await process.wait()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    if process.returncode and not turn.error:
        stderr = (await process.stderr.read()).decode(errors="replace") if process.stderr else ""
        turn.error = stderr.strip()[-600:] or f"codex exited with {process.returncode}"
    if PLAN_READY in turn.text:
        turn.text = turn.plan = turn.text.replace(PLAN_READY, "").strip()
    else:
        turn.text, turn.questions = parse_questions(turn.text)
    return turn
