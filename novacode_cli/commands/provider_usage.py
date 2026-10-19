"""/usage: what is left on every plan or key Nova can read usage for.

* Claude Code and Codex: :mod:`planner_usage` (a saved reading, the session files).
* Command Code: ``/alpha/billing/credits`` and ``/subscriptions``, the routes its
  own ``cmd`` CLI uses. Undocumented, so any change reads as "unavailable".
* OpenCode Go: ``/zen/go/v1/usage`` (undocumented; 5-hour, weekly, monthly).
* OpenRouter: ``/api/v1/key`` and ``/api/v1/credits`` (documented).

Anthropic and OpenAI report usage only to admin keys, and Gemini, NVIDIA and
Ollama have no usage endpoint, so they are not shown.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime

from novacode_cli.commands.planner_usage import Usage, claude_usage, codex_usage

TIMEOUT = 15


def _get(url: str, key: str) -> dict:
    import httpx

    response = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _epoch(iso: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _key(env_var: str) -> str:
    from novacode_cli.config.credentials import credential_value

    return credential_value(env_var)


def commandcode_usage(key: str) -> Usage:
    """Credits, the 5-hour and weekly windows, and the plan."""
    base = "https://api.commandcode.ai/alpha/billing"
    data = _get(f"{base}/credits", key)
    limits = data.get("windowLimits") or {}
    windows, spent = [], []
    for label, name in (("5-hour", "fiveHour"), ("weekly", "weekly")):
        window = limits.get(name) or {}
        cap = window.get("cap") or 0
        if cap:
            used = float(window.get("used") or 0)
            reset = window.get("resetAt")
            windows.append((label, used / cap, int(reset) // 1000 if reset else None))
            spent.append(f"{label} ${used:.2f} of ${cap:g}")
    credits = data.get("credits") or {}
    left = [
        f"${float(credits[name]):.2f} {label}"
        for name, label in (
            ("monthlyCredits", "monthly credits"),
            ("purchasedCredits", "purchased"),
            ("freeCredits", "free"),
        )
        if credits.get(name)
    ]
    lines = []
    if left:
        lines.append("Left: " + " · ".join(left))
    if spent:
        lines.append("Spent: " + " · ".join(spent))
    try:  # the plan is a nicety; the credits above are the reading
        plan = _get(f"{base}/subscriptions", key).get("data") or {}
        name = str(plan.get("planId") or "").removeprefix("individual-").split("-v")[0]
        renews = _epoch(plan.get("currentPeriodEnd"))
        if name:
            when = f", renews {datetime.fromtimestamp(renews):%d %b}" if renews else ""
            lines.append(f"Plan: {name.title()} ({plan.get('status', '?')}{when})")
    except Exception:  # noqa: BLE001
        pass
    exceeded = any(used >= 1 for _, used, _ in windows) or limits.get("exceeded")
    return Usage("Command Code", windows, "rejected" if exceeded else "", time.time(), lines=lines)


def opencode_go_usage(key: str) -> Usage:
    """OpenCode Go's rolling 5-hour, weekly and monthly windows."""
    data = _get("https://opencode.ai/zen/go/v1/usage", key).get("usage") or {}
    windows, limited = [], False
    for label, name in (("5-hour", "rolling"), ("weekly", "weekly"), ("monthly", "monthly")):
        window = data.get(name)
        if isinstance(window, dict):
            windows.append((label, float(window.get("percent") or 0) / 100, _epoch(window.get("resetsAt"))))
            limited = limited or window.get("status", "ok") != "ok"
    return Usage("OpenCode Go", windows, "rejected" if limited else "", time.time())


def openrouter_usage(key: str) -> Usage:
    """This key's spend and limit, the account's credits, free-model requests."""
    data = _get("https://openrouter.ai/api/v1/key", key).get("data") or {}
    windows = []
    if data.get("limit"):
        windows.append(("key limit", float(data.get("usage") or 0) / float(data["limit"]), None))
    free = data.get("free_model_daily_requests") or {}
    lines = []
    if free.get("limit"):
        windows.append(("free", float(free.get("used") or 0) / float(free["limit"]), None))
        lines.append(f"Free-model requests today: {free.get('used') or 0} of {free['limit']}")
    lines += [
        "Spent: today ${:.2f} · week ${:.2f} · month ${:.2f}".format(
            *(float(data.get(f) or 0) for f in ("usage_daily", "usage_weekly", "usage_monthly"))
        )
    ]
    try:
        credits = _get("https://openrouter.ai/api/v1/credits", key).get("data") or {}
        bought, used = float(credits.get("total_credits") or 0), float(credits.get("total_usage") or 0)
        lines.append(f"Credits: ${max(bought - used, 0):.2f} left (${bought:.2f} bought, ${used:.2f} used)")
    except Exception:  # noqa: BLE001
        pass
    return Usage("OpenRouter", windows, "", time.time(), lines=lines)


#: name, key variable(s), reader. A provider is shown only when it has a key.
_PROVIDERS = (
    ("Command Code", ("COMMANDCODE_API_KEY", "CMD_API_KEY"), commandcode_usage),
    ("OpenCode Go", ("OPENCODE_API_KEY",), opencode_go_usage),
    ("OpenRouter", ("OPENROUTER_API_KEY",), openrouter_usage),
)


def _read(name: str, reader, key: str) -> Usage:  # noqa: ANN001
    try:
        return reader(key)
    except Exception as exc:  # noqa: BLE001 - one provider never hides the others
        status = getattr(getattr(exc, "response", None), "status_code", None)
        why = f"HTTP {status}" if status else type(exc).__name__
        return Usage(name, note=f"Usage unavailable ({why}).")


def all_usage() -> list[Usage]:
    """Every readable usage, in a fixed order. Blocking: run it in a thread."""
    from concurrent.futures import ThreadPoolExecutor

    found: list[Usage] = []
    claude = claude_usage()
    if claude.windows or shutil.which("claude"):
        found.append(claude)
    codex = codex_usage()
    if codex.windows or shutil.which("codex"):
        found.append(codex)
    jobs = []
    for name, env_vars, reader in _PROVIDERS:
        key = next((k for v in env_vars if (k := _key(v) or os.environ.get(v, ""))), "")
        if key:
            jobs.append((name, reader, key))
    with ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
        found += list(pool.map(lambda job: _read(*job), jobs))
    return found
