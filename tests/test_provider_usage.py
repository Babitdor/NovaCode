"""/usage: each provider's reply, as captured live on 2026-10-19, read into bars."""

from __future__ import annotations

import pytest

from novacode_cli.commands import provider_usage as pu
from novacode_cli.commands.planner_usage import Usage, format_usage

REPLIES = {
    "https://api.commandcode.ai/alpha/billing/credits": {
        "credits": {"monthlyCredits": 79.99, "purchasedCredits": 0, "freeCredits": 0},
        "windowLimits": {
            "limited": True,
            "exceeded": None,
            "fiveHour": {"used": 4, "cap": 16, "exceeded": False, "resetAt": 1792413552347},
            "weekly": {"used": 40, "cap": 40, "exceeded": True, "resetAt": 1793000352347},
        },
    },
    "https://api.commandcode.ai/alpha/billing/subscriptions": {
        "data": {"status": "active", "planId": "individual-pro-v1", "currentPeriodEnd": "2026-11-19T07:01:31.000Z"}
    },
    "https://opencode.ai/zen/go/v1/usage": {
        "usage": {
            "rolling": {"status": "ok", "percent": 25, "resetsAt": "2026-10-19T12:43:59.251Z"},
            "weekly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-26T00:00:00.000Z"},
            "monthly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-10-23T18:17:59.000Z"},
        }
    },
    "https://openrouter.ai/api/v1/key": {
        "data": {
            "limit": 10, "usage": 2.5, "usage_daily": 0.5, "usage_weekly": 1, "usage_monthly": 2.5,
            "free_model_daily_requests": {"used": 5, "limit": 50, "remaining": 45},
        }
    },
    "https://openrouter.ai/api/v1/credits": {"data": {"total_credits": 20, "total_usage": 2.5}},
}


@pytest.fixture(autouse=True)
def replies(monkeypatch):
    monkeypatch.setattr(pu, "_get", lambda url, key: REPLIES[url])


def test_command_code_shows_windows_credits_and_plan():
    usage = pu.commandcode_usage("k")
    assert usage.windows == [("5-hour", 0.25, 1792413552), ("weekly", 1.0, 1793000352)]
    assert usage.status == "rejected", "the weekly cap is spent"
    text = format_usage(usage)
    assert "Left: $79.99 monthly credits" in text
    assert "weekly $40.00 of $40" in text
    assert "Plan: Pro (active, renews 19 Nov)" in text


def test_opencode_go_reads_three_windows_and_flags_a_limited_one():
    usage = pu.opencode_go_usage("k")
    assert [(label, used) for label, used, _ in usage.windows] == [
        ("5-hour", 0.25), ("weekly", 0.0), ("monthly", 1.0)
    ]
    assert usage.status == "rejected"
    assert all(reset for _, _, reset in usage.windows)


def test_openrouter_reads_key_limit_spend_and_credits():
    usage = pu.openrouter_usage("k")
    assert ("key limit", 0.25, None) in usage.windows
    text = format_usage(usage)
    assert "today $0.50 · week $1.00 · month $2.50" in text
    assert "Credits: $17.50 left ($20.00 bought, $2.50 used)" in text
    assert "Free-model requests today: 5 of 50" in text


def test_a_failing_provider_says_so_without_hiding_the_rest():
    import httpx

    def refuse(key):
        request = httpx.Request("GET", "https://x")
        raise httpx.HTTPStatusError("no", request=request, response=httpx.Response(401, request=request))

    assert pu._read("OpenRouter", refuse, "k").note == "Usage unavailable (HTTP 401)."


def test_only_providers_with_a_key_are_asked(monkeypatch):
    monkeypatch.setattr(pu, "claude_usage", lambda: Usage("Claude Code"))
    monkeypatch.setattr(pu, "codex_usage", lambda: Usage("Codex"))
    monkeypatch.setattr(pu.shutil, "which", lambda _name: None)
    monkeypatch.setattr(pu, "_key", lambda var: "k" if var == "OPENCODE_API_KEY" else "")
    for var in ("COMMANDCODE_API_KEY", "CMD_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert [usage.name for usage in pu.all_usage()] == ["OpenCode Go"]


@pytest.mark.asyncio
async def test_usage_opens_a_modal_and_settings_no_longer_shows_it(monkeypatch):
    from textual.widgets import Static

    from novacode_cli.tui.screens import SettingsScreen, UsageScreen
    from tests.test_tui_compaction_row import _app

    monkeypatch.setattr(pu, "all_usage", lambda: [pu.opencode_go_usage("k")])
    app = _app()
    async with app.run_test(size=(120, 50)) as pilot:
        await app._run_usage()
        await pilot.pause(0.5)
        screen = app.screen
        assert isinstance(screen, UsageScreen)
        body = str(screen.query_one("#usage-body", Static).render())
        assert "OpenCode Go" in body and "monthly" in body
        await pilot.press("escape")
        await pilot.pause()
        app.push_screen(SettingsScreen())
        await pilot.pause()
        assert not app.screen.query("#planner-usage")
