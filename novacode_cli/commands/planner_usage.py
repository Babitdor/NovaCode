"""How much of their plans' usage /claude-plan and /codex-plan have left.

* Codex writes its rate limits into every session file under
  ``~/.codex/sessions`` (``rate_limits``: a 5-hour and a weekly window), so the
  newest file is read, with no call.
* Claude Code reports them only during a call (``rate_limit_event`` in
  ``stream-json``): /claude-plan records each one it sees, and
  :func:`check_claude_now` makes one tiny call for a fresh reading.

Readings are kept in ``~/.nova/planner_usage.json``, and the planning sessions
an interrupted run can resume (``/claude-plan resume``) in
``~/.nova/planner_sessions.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.text import Text

_STORE = Path.home() / ".nova" / "planner_usage.json"
_CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
_PLANS = Path.home() / ".nova" / "planner_sessions.json"


def _plans() -> dict:
    try:
        return json.loads(_PLANS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_plans(data: dict) -> None:
    with contextlib.suppress(OSError):
        _PLANS.parent.mkdir(parents=True, exist_ok=True)
        _PLANS.write_text(json.dumps(data), encoding="utf-8")


def remember_session(command: str, root: str, session_id: str, task: str) -> None:
    """Keep the planning session of ``command`` in ``root`` until it finishes."""
    data = _plans()
    data[f"{command}|{root}"] = {"session": session_id, "task": task, "at": time.time()}
    _save_plans(data)


def interrupted(command: str, root: str) -> dict | None:
    """The unfinished session (``session``, ``task``) of ``command`` in ``root``."""
    return _plans().get(f"{command}|{root}")


def forget_session(command: str, root: str) -> None:
    data = _plans()
    if data.pop(f"{command}|{root}", None) is not None:
        _save_plans(data)


@dataclass
class Usage:
    """One planner's usage: each window's share used and when it resets."""

    name: str
    windows: list[tuple[str, float, int | None]] = field(default_factory=list)  # label, 0-1, epoch
    status: str = ""  # Claude: allowed / allowed_warning / rejected
    checked_at: float | None = None
    note: str = ""  # why there is no reading
    lines: list[str] = field(default_factory=list)  # extra facts: credits, plan, spend


def _window_label(key: str) -> str:
    return {"five_hour": "5-hour", "seven_day": "weekly", "primary": "5-hour", "secondary": "weekly"}.get(
        key, key.replace("_", " ")
    )


def _load() -> dict:
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_claude(info: dict) -> None:
    """Keep a ``rate_limit_info`` from a Claude Code run."""
    if not isinstance(info, dict):
        return
    data = _load()
    data["claude"] = {"info": info, "checked_at": time.time()}
    with contextlib.suppress(OSError):
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps(data), encoding="utf-8")


def claude_usage() -> Usage:
    """The last Claude Code reading Nova saw."""
    saved = _load().get("claude")
    if not saved:
        return Usage("Claude Code", note="No reading yet: run /claude-plan or press Check Claude now.")
    info = saved.get("info") or {}
    windows = [
        (_window_label(key), float(w.get("utilization") or 0), w.get("resetsAt"))
        for key, w in (info.get("unifiedWindows") or {}).items()
        if isinstance(w, dict)
    ]
    return Usage("Claude Code", windows, str(info.get("status") or ""), saved.get("checked_at"))


def codex_usage(sessions: Path = _CODEX_SESSIONS) -> Usage:
    """Codex's limits from its newest session file (no call)."""
    files = sorted(sessions.glob("*/*/*/*.jsonl"), key=lambda p: p.stat().st_mtime) if sessions.is_dir() else []
    for path in reversed(files[-5:]):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:  # Codex appends live: the last line can be half-written
                limits = _find(json.loads(line), "rate_limits")
            except ValueError:
                continue
            if not isinstance(limits, dict):
                continue
            windows = [
                (_window_label(key), float(w.get("used_percent") or 0) / 100, w.get("resets_at"))
                for key in ("primary", "secondary")
                if isinstance(w := limits.get(key), dict)
            ]
            credits = limits.get("credits") or {}
            status = "out of credits" if credits.get("has_credits") is False and any(u >= 1 for _, u, _ in windows) else ""
            return Usage("Codex", windows, status, path.stat().st_mtime)
    return Usage("Codex", note="No reading yet: Codex records it once you use it (codex or /codex-plan).")


def _find(value: object, key: str) -> object:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for inner in value.values():
            found = _find(inner, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for inner in value:
            found = _find(inner, key)
            if found is not None:
                return found
    return None


async def check_claude_now() -> Usage:
    """One tiny Claude Code call for a fresh reading (uses a sliver of the quota)."""
    from novacode_cli.commands.claude_plan import ANTHROPIC_VARS, cli_env, executable

    exe = executable()
    if exe is None:
        return Usage("Claude Code", note="Claude Code is not installed.")
    process = await asyncio.create_subprocess_exec(
        exe, "-p", "Reply with: ok", "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config",
        env=cli_env(ANTHROPIC_VARS)[0],
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None

    seen = False

    async def read() -> None:
        nonlocal seen
        async for raw in process.stdout:
            with contextlib.suppress(ValueError):
                event = json.loads(raw)
                if event.get("type") == "rate_limit_event":
                    record_claude(event.get("rate_limit_info") or {})
                    seen = True
        await process.wait()

    try:
        await asyncio.wait_for(read(), timeout=90)
    except TimeoutError:
        pass  # the last reading stays
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()  # reap it
    usage = claude_usage()
    if not seen:
        usage.status = "check failed"
        usage.note = "The check got no reading (is Claude Code logged in?)."
    return usage


BAR_WIDTH = 30


def _bar_color(used: float) -> str:
    return "green" if used < 0.6 else "yellow" if used < 0.85 else "red"


def usage_text(usage: Usage) -> Text:
    """The planner's name, then one progress bar per window::

        Claude Code  nearing a limit                 as of Sat 18:26
          5-hour  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  59%  resets 21:50
    """
    text = Text(usage.name, style="bold")
    if not usage.windows and not usage.lines:
        return text.append(f"  {usage.note}", style="dim")
    if usage.status in ("rejected", "out of credits"):
        text.append("  LIMIT REACHED", style="bold red")
    elif usage.status == "allowed_warning":
        text.append("  nearing a limit", style="yellow")
    elif usage.status == "check failed":
        text.append(f"  {usage.note} Showing the last reading.", style="red")
    if usage.checked_at:
        text.append(f"  as of {datetime.fromtimestamp(usage.checked_at):%a %H:%M}", style="dim")
    for label, used, resets in usage.windows:
        when = ""
        if resets and int(resets) <= time.time():
            used, when = 0.0, "reset since"  # the reading predates the reset
        elif resets:
            moment = datetime.fromtimestamp(int(resets))
            when = f"resets {moment:%H:%M}" if moment.date() == datetime.now().date() else f"resets {moment:%a %H:%M}"
        filled = round(min(max(used, 0.0), 1.0) * BAR_WIDTH)
        text.append(f"\n  {label:<7} ")
        text.append("━" * filled, style=_bar_color(used))
        text.append("━" * (BAR_WIDTH - filled), style="grey35")
        text.append(f" {round(used * 100):>3}% used", style=f"bold {_bar_color(used)}")
        text.append(f"  {when}" if when else "", style="dim")
    for line in usage.lines:
        text.append(f"\n  {line}", style="dim")
    return text


def format_usage(usage: Usage) -> str:
    return usage_text(usage).plain


if __name__ == "__main__":  # pragma: no cover - a quick look from a terminal
    print(format_usage(codex_usage()))
    print(format_usage(claude_usage()))
