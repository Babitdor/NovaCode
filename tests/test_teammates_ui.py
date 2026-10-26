"""Teammate review remains usable without losing drafts or approval identity."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Input, Select, TabbedContent, TextArea

from novacode_cli.tui import teammates


async def test_model_settings_load_save_and_preserve_drafts(monkeypatch: pytest.MonkeyPatch):
    state = {
        "name": "Ada",
        "role": "Assistant",
        "computer": {"phase": "suspended"},
        "model": {"provider": "openai", "model": "saved-custom-model"},
        "vision": {"provider": "anthropic", "model": "saved-custom-vision"},
    }
    edits = []

    async def request(action: str, payload: dict | None = None) -> dict:
        if action == "edit":
            edits.append(payload)
        return copy.deepcopy(state)

    monkeypatch.setattr(teammates, "request", request)
    app = App()
    async with app.run_test(size=(80, 32)) as pilot:
        screen = teammates.TeammatesScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#teammate-tabs", TabbedContent).active = "settings-tab"
        await pilot.pause()
        assert screen.query_one("#teammate-model", Input).value == "saved-custom-model"
        screen.query_one("#teammate-provider", Select).value = "anthropic"
        await pilot.pause()
        screen.query_one("#teammate-model", Input).value = "edited-custom-model"
        await screen.reload().wait()
        assert screen.query_one("#teammate-model", Input).value == "edited-custom-model"
        button = screen.query_one("#edit", Button)
        button.scroll_visible()
        await pilot.pause()
        await pilot.click(button)
        await pilot.pause()
        assert edits[0]["model"] == {"provider": "anthropic", "model": "edited-custom-model"}
        assert edits[0]["vision"] == state["vision"]


@pytest.mark.parametrize("size", [(100, 40), (60, 30)])
async def test_review_controls_stay_visible_and_refresh_preserves_edits(
    monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
):
    state = {
        "name": "Ada",
        "role": "Assistant",
        "task_state": "running",
        "computer": {"phase": "ready"},
        "tasks": [{"id": "old-task", "status": "completed", "text": "First task"}],
        "approvals": [
            {
                "id": "exact-proposal",
                "state": "pending",
                "task": "active-task",
                "screenshot": "exact-frame",
                "args": {"action": "type", "text": "Review this long input.\n" * 40},
            }
        ],
    }
    decisions = []

    async def request(action: str, payload: dict | None = None) -> dict:
        if action == "decide":
            decisions.append(payload)
            state["approvals"] = []
        return copy.deepcopy(state)

    monkeypatch.setattr(teammates, "request", request)
    app = App()
    async with app.run_test(size=size) as pilot:
        screen = teammates.TeammatesScreen()
        app.push_screen(screen)
        await pilot.pause()
        await screen.reload().wait()
        assert screen.loaded, str(screen.query_one("#teammate-error").render())
        task_input = screen.query_one("#teammate-task", TextArea)
        task_input.load_text("Unsent task draft")
        screen.query_one("#teammate-role", TextArea).load_text("Edited role")
        await screen.reload().wait()
        assert task_input.text == "Unsent task draft"
        assert screen.query_one("#teammate-role", TextArea).text == "Edited role"
        approve = screen.query_one("#approve", Button)
        assert approve.region.bottom <= screen.query_one("#teammate-panel").region.bottom
        await pilot.click("#approve")
        await pilot.pause()
        assert decisions == [{"id": "exact-proposal", "approve": True}]

        screen.query_one("#teammate-tabs", TabbedContent).active = "history-tab"
        await pilot.pause()
        selector = screen.query_one("#history-task", Select)
        selector.value = "old-task"
        state["tasks"].append({"id": "new-task", "status": "queued", "text": "Second task"})
        await screen.reload().wait()
        assert selector.value == "old-task"
        assert task_input.text == "Unsent task draft"
        await pilot.press("escape")


async def test_empty_profile_opens_setup(monkeypatch: pytest.MonkeyPatch):
    created = []

    async def request(action: str, payload: dict | None = None) -> dict:
        if action == "create":
            created.append(payload)
        return {"computer": None}

    monkeypatch.setattr(teammates, "request", request)
    app = App()
    async with app.run_test(size=(80, 32)) as pilot:
        screen = teammates.TeammatesScreen()
        app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#teammate-tabs", TabbedContent).active == "settings-tab"
        assert screen.query_one("#create", Button).display
        assert screen.query_one("#assign", Button).disabled
        screen.query_one("#teammate-name", Input).value = "Ada"
        await pilot.click("#create")
        await pilot.pause()
        assert created == [{"name": "Ada"}]


@pytest.mark.parametrize(
    "style",
    ["conversation-style", "conversation-style minimal-ui", "conversation-style terminal-ui"],
)
async def test_editors_remain_boxed_under_nova_modal_styles(
    monkeypatch: pytest.MonkeyPatch, style: str
):
    from novacode_cli.tui import app as nova_app
    from novacode_cli.tui.widgets import NOVA_TOKYO_NIGHT

    class StyledApp(App):
        CSS = nova_app.NovaApp.CSS
        CSS_PATH = str(Path(nova_app.__file__).with_name("interface_styles.tcss"))

    async def request(_action: str) -> dict:
        return {"name": "Ada", "role": "Assistant", "computer": {"phase": "suspended"}}

    monkeypatch.setattr(teammates, "request", request)
    app = StyledApp()
    app.register_theme(NOVA_TOKYO_NIGHT)
    app.theme = "tokyo-night"
    async with app.run_test(size=(80, 32)) as pilot:
        screen = teammates.TeammatesScreen()
        screen.set_classes(style)
        app.push_screen(screen)
        await pilot.pause()
        task = screen.query_one("#teammate-task", TextArea)
        assert task.styles.border_top[0] == "round"
        assert task.styles.background.a > 0
        assert task.content_region.height >= 3
        task.focus()
        await pilot.press("h", "i", "enter", "x")
        assert task.text == "hi\nx"
        tabs = screen.query_one("#teammate-tabs", TabbedContent)
        await pilot.click(tabs.get_tab("settings-tab"))
        await pilot.pause()
        name = screen.query_one("#teammate-name")
        role = screen.query_one("#teammate-role", TextArea)
        assert name.styles.border_top[0] == "round"
        assert name.region.height >= 3
        assert role.styles.border_top[0] == "round"
        assert role.content_region.height >= 3
        for field in ("#teammate-model", "#teammate-vision-model"):
            editor = screen.query_one(field, Input)
            assert editor.styles.border_top[0] == "round"
            assert editor.styles.height.value >= 3
        save = screen.query_one("#edit", Button)
        assert screen.query_one("#teammate-panel").region.contains_region(save.region)
        name.focus()
        await pilot.press("end", "z")
        assert name.value == "Adaz"
        role.focus()
        await pilot.press("z")
        assert role.text == "zAssistant"
