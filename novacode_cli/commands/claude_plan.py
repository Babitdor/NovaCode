"""/claude-plan: Claude Code plans headless; Nova approves and implements.

Each turn runs ``claude -p --permission-mode plan`` in the project and reads its
``stream-json`` output. Headless Claude has no ``ExitPlanMode`` or question
tool, so the protocol is in the appended instructions: questions end a turn as
plain text, and a finished plan is written to its plan file (Claude's own plan
mode puts it under ``~/.claude/plans/``) with the turn ending in ``PLAN_READY``.
Answers and refinements resume the same Claude session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from novacode_cli.commands import planner_usage

PLAN_READY = "PLAN_READY"
QUESTIONS_TAG = "nova-questions"
#: How the planners ask: the shape of Nova's ask_user_question tool.
QUESTIONS_PROTOCOL = f"""\
- If you need decisions or facts only the user has, do not write the plan yet.
  End your turn with the questions in a fenced code block tagged {QUESTIONS_TAG}
  holding a JSON list, one entry per question:
  ```{QUESTIONS_TAG}
  [{{"question": "Which database?", "context": "why you ask, one sentence",
    "options": [{{"label": "Postgres", "description": "what it implies"}},
                {{"label": "SQLite", "description": "..."}}]}}]
  ```
  Give 2-4 options per question; the user can always answer in their own words."""
NAME = "Claude Code"
COMMAND = "/claude-plan"
DOCS_URL = "https://docs.claude.com/en/docs/claude-code"
COLOR = "#d97757"
INSTALL_LABEL = "official installer"

INSTRUCTIONS = f"""\
You are planning for another coding agent (Nova), which will implement your plan.
You run headless: no interactive tools, the user answers only between your turns.
- Explore the repository read-only as much as you need.
{QUESTIONS_PROTOCOL}
- When the plan is complete, write it to your plan file as markdown: context,
  the concrete steps with the files to change, and how to verify. Then end your
  turn with a one-paragraph summary followed by a line containing only {PLAN_READY}.
"""
#: Plan mode: read-only exploration; questions come back as text, not the tool.
PLAN_TOOLS = ["--permission-mode", "plan", "--disallowedTools", "AskUserQuestion"]

#: /claude-review and /codex-review: what the reviewer is told (both CLIs).
REVIEW_INSTRUCTIONS = """\
You are reviewing code for another coding agent (Nova), which will report your
findings to the user and apply the fixes they accept. You run headless; nobody
answers questions. Do not modify any files.
- Find what to review: by default the uncommitted changes (`git status`,
  `git diff HEAD`, and any untracked files); if there are none, the last commit
  (`git show HEAD`). Read the surrounding code to judge them.
- Look for bugs, regressions, security problems, missing error handling and
  tests, and needless complexity. Report only real problems, no style nits.
- Reply with a markdown report:
  ## Scope - what you reviewed, one line.
  ## Findings - each one as: **[high|medium|low]** `path:line` - the problem,
     why it matters, and the fix (a short diff when it helps). Most severe first.
  ## Verdict - one or two sentences: ready to ship or not.
  If you found nothing, say so under Findings.
"""
#: Review without edits: read, search and git's read-only commands only. In
#: -p mode a tool that is not allowed is denied, not prompted.
REVIEW_TOOLS = [
    "--permission-mode", "default",
    "--allowedTools",
    "Read,Grep,Glob,Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*)",
    "--disallowedTools", "AskUserQuestion,Write,Edit,MultiEdit,NotebookEdit",
]
REVIEW_COMMAND = "/claude-review"
REVIEW_ARGS = {"instructions": REVIEW_INSTRUCTIONS, "tool_args": REVIEW_TOOLS}


@dataclass
class PlanTurn:
    """The outcome of one headless planning turn."""

    session_id: str | None = None
    text: str = ""  # the turn's final message
    plan: str | None = None  # the plan file's content, once the plan is ready
    error: str | None = None
    #: The turn's questions, ask_user_question style: question, options, context.
    questions: list[dict] = field(default_factory=list)


_QUESTIONS_BLOCK = re.compile(rf"```{QUESTIONS_TAG}\s*(.*?)```", re.DOTALL)


def parse_questions(text: str) -> tuple[str, list[dict]]:
    """Split a planner's reply into its prose and its ``nova-questions`` entries.

    An entry needs a question and at least two options (ask_user_question's
    rule); a malformed block leaves the reply as prose, asked as one question.
    """
    match = _QUESTIONS_BLOCK.search(text)
    if not match:
        return text, []
    try:
        raw = json.loads(match.group(1))
    except ValueError:
        return text, []
    questions = [
        {
            "question": str(entry["question"]),
            "options": [o for o in entry.get("options") or [] if isinstance(o, (str, dict))],
            "context": str(entry.get("context") or "") or None,
        }
        for entry in (raw if isinstance(raw, list) else [raw])
        if isinstance(entry, dict) and entry.get("question")
    ]
    questions = [q for q in questions if len(q["options"]) >= 2]
    if not questions:
        return text, []
    return (text[: match.start()] + text[match.end() :]).strip(), questions


#: Shown when Claude Code is missing or not logged in.
LOGIN_HINT = (
    "Log in once: run `claude` in a terminal and follow the prompt "
    "(or set ANTHROPIC_API_KEY), then run /claude-plan again."
)


def executable() -> str | None:
    """The ``claude`` CLI, if installed.

    Also looks where the official installer puts it (``~/.local/bin``): right
    after an install, this process's PATH does not have it yet.
    """
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    return str(local) if local.is_file() else None


def install_command() -> list[str]:
    """Claude Code's official installer for this platform."""
    if sys.platform == "win32":
        return [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "irm https://claude.ai/install.ps1 | iex",
        ]
    return ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]


async def install(on_line: Callable[[str], None]) -> bool:
    """Run the official installer, streaming its output; True when ``claude`` is then found."""
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


def needs_login(error: str) -> bool:
    low = error.lower()
    return any(s in low for s in ("/login", "log in", "not logged", "api key", "authenticat"))


#: Provider settings Nova exports into its own environment (``SecretManager``
#: hydrates every stored key at startup). Inherited, they override the CLI's
#: own login: ``claude`` billed Nova's Anthropic API key ("Credit balance is
#: too low") instead of the user's claude.ai subscription.
ANTHROPIC_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def cli_env(variables: tuple[str, ...], *, keep: bool = False) -> tuple[dict[str, str], bool]:
    """The environment to run a planner CLI in, and whether it held one of ``variables``.

    ``keep=False`` leaves ``variables`` out, so the CLI uses its own login.
    """
    present = any(os.environ.get(name) for name in variables)
    if keep:
        return dict(os.environ), present
    return {k: v for k, v in os.environ.items() if k not in variables}, present


def _brief(name: str, args: dict) -> str:
    """One line for a tool call: ``Read src/app.py``, ``Grep "def main"``."""
    for key in ("file_path", "path", "pattern", "command", "url", "query", "description"):
        value = args.get(key)
        if value:
            value = " ".join(str(value).split())
            return f"{name} {value[:90]}"
    return name


def _is_plan_file(path: str) -> bool:
    parts = Path(path).parts
    return ".claude" in parts and "plans" in parts and path.endswith(".md")


async def run_turn(
    prompt: str,
    cwd: Path,
    *,
    session_id: str | None = None,
    on_step: Callable[[str], None] | None = None,
    on_session: Callable[[str], None] | None = None,
    instructions: str = INSTRUCTIONS,
    tool_args: list[str] = PLAN_TOOLS,
) -> PlanTurn:
    """Run one headless planning turn; ``on_step`` receives each tool call's line.

    Runs on the user's Claude Code login. Only when there is none, and Nova
    has an Anthropic key, is the turn retried with that key.
    """
    env, has_key = cli_env(ANTHROPIC_VARS)
    extra = {"on_session": on_session, "instructions": instructions, "tool_args": tool_args}
    turn = await _run(prompt, cwd, session_id=session_id, on_step=on_step, env=env, **extra)
    if turn.error and has_key and needs_login(turn.error):
        turn = await _run(
            prompt, cwd, session_id=session_id, on_step=on_step, **extra,
            env=cli_env(ANTHROPIC_VARS, keep=True)[0],
        )
    if turn.error and "credit balance" in turn.error.lower():
        turn.error = (
            "The Anthropic API key has no credit left, and Claude Code has no login of "
            f"its own to use instead. {LOGIN_HINT}"
        )
    elif turn.error and needs_login(turn.error):
        turn.error = f"Claude Code is not logged in. {LOGIN_HINT}\n({turn.error[:200]})"
    return turn


async def _run(
    prompt: str,
    cwd: Path,
    *,
    session_id: str | None,
    on_step: Callable[[str], None] | None,
    env: dict[str, str],
    on_session: Callable[[str], None] | None = None,
    instructions: str = INSTRUCTIONS,
    tool_args: list[str] = PLAN_TOOLS,
) -> PlanTurn:
    exe = executable()
    if exe is None:
        return PlanTurn(error="Claude Code is not installed (no `claude` found).")
    command = [
        exe, "-p", prompt,
        *tool_args,
        "--output-format", "stream-json", "--verbose",
        "--append-system-prompt", instructions,
        # Your MCP servers are not needed to plan, and a slow one delays every turn.
        "--strict-mcp-config",
    ]
    if session_id:
        command += ["--resume", session_id]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # else it waits 3 s for piped input
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # one JSON event per line; a written plan is one event
    )
    turn = PlanTurn(session_id=session_id)
    plan_path: str | None = None
    try:
        assert process.stdout is not None
        async for raw in process.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                turn.session_id = event.get("session_id") or turn.session_id
                if turn.session_id and on_session is not None:
                    on_session(turn.session_id)  # known now, so an interrupted run can resume
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") != "tool_use":
                        continue
                    args = block.get("input") or {}
                    path = str(args.get("file_path") or "")
                    if block.get("name") in {"Write", "Edit", "MultiEdit"} and _is_plan_file(path):
                        plan_path = path
                    elif on_step is not None:
                        on_step(_brief(str(block.get("name")), args))
            elif kind == "rate_limit_event":
                planner_usage.record_claude(event.get("rate_limit_info") or {})
            elif kind == "result":
                turn.session_id = event.get("session_id") or turn.session_id
                turn.text = str(event.get("result") or "")
                if event.get("is_error"):
                    turn.error = turn.text or f"Claude ended with {event.get('subtype')}"
        await process.wait()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    if process.returncode and not turn.error:
        stderr = (await process.stderr.read()).decode(errors="replace") if process.stderr else ""
        turn.error = stderr.strip()[-600:] or f"claude exited with {process.returncode}"
    if PLAN_READY in turn.text:
        turn.text = turn.text.replace(PLAN_READY, "").strip()
        if plan_path:
            with contextlib.suppress(OSError):
                turn.plan = Path(plan_path).read_text(encoding="utf-8")
        turn.plan = turn.plan or turn.text  # no plan file: the summary is the plan
    else:
        turn.text, turn.questions = parse_questions(turn.text)
    return turn
