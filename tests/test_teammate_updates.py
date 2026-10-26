"""Teammate results remain readable and owner-visible after the panel closes."""

from __future__ import annotations

import copy

import pytest
from rich.markdown import Markdown
from textual.widgets import Collapsible, Label, Select, Static, TabbedContent

from novacode_cli.tracking.usage_tree import UsageTree
from novacode_cli.tui import teammate_updates, teammates
from tests.test_tui_home_preferences import _app


def task_record(task_id: str = "task-1") -> dict:
    turn = UsageTree("private-thread")
    turn.record(
        ("main",),
        "gpt-5-mini",
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 300,
        },
    )
    vision = UsageTree("private-thread")
    vision.record(
        ("vision-preflight",),
        "gpt-5-mini",
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_tokens": 20,
        },
    )
    return {
        "id": task_id,
        "text": "Compare **Python libraries**",
        "status": "completed",
        "full_output": (
            "## Findings\n\n- **First** result\n- Second result\n\n```python\nprint(1)\n```"
        ),
        "usage": turn.to_dict(),  # Legacy latest-turn mirror must not be counted twice.
        "usage_history": {"turn-1": turn.to_dict(), "vision-1": vision.to_dict()},
    }


def test_usage_totals_include_vision_and_resume_without_double_counting():
    task = task_record()
    retry = UsageTree("private-thread")
    retry.record(("main",), "gpt-5-mini", {"input_tokens": 200, "output_tokens": 40})
    task["usage_history"]["resumed-turn"] = retry.to_dict()
    summary, details = teammate_updates.usage_markdown([task])
    assert "3 model calls" in summary
    assert "1,590 tokens" in summary
    assert "Input: **1,300**" in summary
    assert "Output: **290**" in summary
    assert "Cache read: **300**" in summary
    assert "Cache creation: **20**" in summary
    assert "vision-preflight" in details
    assert "gpt-5-mini" in details
    assert "private-thread" not in details
    total, _ = teammate_updates.usage_markdown([task, task_record("task-2")])
    assert "5 model calls" in total
    legacy, _ = teammate_updates.usage_markdown([{"usage": task["usage"]}])
    assert "1 model call" in legacy


def test_unknown_price_and_missing_usage_remain_explicit():
    tree = UsageTree("private")
    tree.record(("main",), "uncatalogued-model", {})
    summary, _ = teammate_updates.usage_markdown([{"usage": tree.to_dict()}])
    assert "Unavailable" in summary
    assert "did not report token usage" in summary
    assert "totals are partial" in summary
    assert teammate_updates.usage_markdown([])[0] == "No model usage recorded yet."


@pytest.mark.parametrize("size", [(100, 40), (60, 30)])
async def test_named_agent_history_renders_markdown_and_usage(
    monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
):
    from textual.app import App

    state = {
        "thread": "ada",
        "name": "Ada",
        "role": "Assistant",
        "tasks": [task_record(), task_record("task-2")],
        "computer": None,
    }

    async def request(_action: str) -> dict:
        return copy.deepcopy(state)

    monkeypatch.setattr(teammates, "request", request)
    app = App()
    async with app.run_test(size=size) as pilot:
        screen = teammates.TeammatesScreen()
        app.push_screen(screen)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        panel = next(iter(screen.panels.values()))
        panel.query_one("#teammate-tabs", TabbedContent).active = "history-tab"
        await pilot.pause()
        assert "Ada" in str(panel.query_one("#history-heading", Label).content)
        body = panel.query_one("#teammate-history", Static).content
        assert isinstance(body, Markdown)
        assert "## Findings" in body.markup
        assert "4 model calls" in panel.query_one("#agent-usage", Static).content.markup
        assert "2 model calls" in panel.query_one("#history-usage", Static).content.markup
        details = panel.query_one("#usage-details", Static).content
        assert isinstance(details, Markdown)
        assert "| Scope | Model |" in details.markup
        panel.query_one("#history-task", Select).value = "task-1"
        await screen.reload().wait()
        assert panel.query_one("#history-task", Select).value == "task-1"


@pytest.mark.timeout(30)
async def test_inline_updates_survive_modal_close_and_preserve_collapse(
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "thread": "ada",
        "name": "Ada",
        "role": "Assistant",
        "tasks": [{**task_record(), "status": "running", "full_output": ""}],
        "computer": None,
        "approvals": [],
    }
    launches = []

    async def request(_action: str, *, launch: bool = True) -> dict:
        launches.append(launch)
        return copy.deepcopy(state)

    monkeypatch.setattr(teammates, "request", request)
    monkeypatch.setattr(teammate_updates, "request", request)
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._run_teammates("/teammates")
        await pilot.pause()
        await app._teammate_feed.refresh()
        await pilot.press("escape")
        await pilot.pause()
        card = app.query_one(".teammate-update", Collapsible)
        assert card.collapsed
        assert "Ada" in str(card.title)
        assert "Running" in str(card.title)
        card.collapsed = False
        state["approvals"] = [{"task": "task-1", "state": "pending", "args": {"text": "PRIVATE"}}]
        await app._teammate_feed.refresh()
        assert "Input approval needed" in str(card.title)
        assert "PRIVATE" not in card.query_one(".teammate-result", Static).content.markup
        state["approvals"] = []
        state["tasks"][0] = task_record()
        await pilot.pause(2.1)  # The app timer keeps updating after the modal closes.
        assert not card.collapsed
        assert "Completed" in str(card.title)
        assert "## Findings" in card.query_one(".teammate-result", Static).content.markup
        await app._teammate_feed.refresh()
        assert len(app.query(".teammate-update")) == 1
        assert False in launches  # Background polling never launches a worker.
        # Two completions between polls each get their own entry.
        state["tasks"].extend([task_record("task-2"), task_record("task-3")])
        await app._teammate_feed.refresh()
        assert len(app.query(".teammate-update")) == 3


@pytest.mark.timeout(30)
async def test_inline_feed_keeps_different_agents_with_matching_task_ids_separate(
    monkeypatch: pytest.MonkeyPatch,
):
    roster = [
        {"name": name, "thread": name.lower(), "tasks": [task_record()]} for name in ("Ada", "Bo")
    ]

    async def request(_action: str, *, launch: bool = True) -> dict:
        assert not launch
        return {"agents": copy.deepcopy(roster)}

    monkeypatch.setattr(teammate_updates, "request", request)
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        feed = teammate_updates.TeammateFeed(app)
        await feed.refresh()
        assert len(app.query(".teammate-update")) == 2
        titles = [str(card.title) for card in app.query(Collapsible)]
        assert any(title.startswith("Ada ·") for title in titles)
        assert any(title.startswith("Bo ·") for title in titles)
        roster[0]["tasks"][0]["status"] = "paused"
        await feed.refresh()
        assert "Paused" in str(feed.cards[("ada", "task-1")].title)
        assert "Completed" in str(feed.cards[("bo", "task-1")].title)
