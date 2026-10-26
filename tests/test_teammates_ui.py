"""Teammate review remains usable without losing drafts or approval identity."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Input, Select, TabbedContent, TextArea

from novacode_cli.tui import teammates


async def test_auto_approve_switch_polling_and_failed_save(monkeypatch: pytest.MonkeyPatch):
    from textual.widgets import Static, Switch

    state = {"agent_id": "ada", "name": "Ada", "role": "Assistant", "auto_approve": True}
    commands = []
    fail = False

    async def request(action: str, payload: dict | None = None) -> dict:
        if action == "status":
            return copy.deepcopy(state) if payload else {"agents": [copy.deepcopy(state)]}
        commands.append((action, payload))
        if fail:
            message = "storage failed"
            raise RuntimeError(message)
        state["auto_approve"] = payload["auto_approve"]
        return copy.deepcopy(state)

    monkeypatch.setattr(teammates, "request", request)
    app = App()
    async with app.run_test(size=(80, 32)) as pilot:
        screen = teammates.TeammatesScreen()
        app.push_screen(screen)
        await pilot.pause()
        await app.workers.wait_for_complete()
        panel = screen.panels["ada"]
        switch = panel.query_one("#auto-approve", Switch)
        assert switch.value
        panel.update_status({**state, "approvals": [{"id": "proposal", "state": "pending"}]})
        assert "Review input" in str(panel.query_one("#approval-status", Static).render())
        for _ in range(3):
            panel.update_status(copy.deepcopy(state))
        await pilot.pause()
        assert commands == []
        switch.value = False
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert commands == [("set_auto_approve", {"agent_id": "ada", "auto_approve": False})]
        assert not switch.value
        fail = True
        switch.value = True
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert not switch.value
        assert "storage failed" in str(panel.query_one("#teammate-error", Static).render())
        assert not switch.disabled
        assert len(commands) == 2


async def test_double_create_does_not_cancel_or_duplicate_the_first_request(
    monkeypatch: pytest.MonkeyPatch,
):
    entered, release = asyncio.Event(), asyncio.Event()
    created = []

    async def request(action: str, payload: dict | None = None) -> dict:
        if action == "status":
            return {"agents": copy.deepcopy(created)}
        entered.set()
        await release.wait()
        created.append({"agent_id": "ada", "name": payload["name"], "role": "Assistant"})
        return created[-1]

    monkeypatch.setattr(teammates, "request", request)
    app = App()
    async with app.run_test(size=(80, 32)) as pilot:
        screen = teammates.TeammatesScreen()
        app.push_screen(screen)
        await pilot.pause()
        await app.workers.wait_for_complete()
        panel = screen.query_one(teammates.TeammatePanel)
        panel.query_one("#teammate-name", Input).value = "Ada"
        button = panel.query_one("#create", Button)
        first = panel.on_button_pressed(Button.Pressed(button))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await panel.on_button_pressed(Button.Pressed(button)).wait()
            assert not created
        finally:
            release.set()
            await first.wait()
        await app.workers.wait_for_complete()
        assert len(created) == 1


@pytest.mark.parametrize("size", [(100, 40), (60, 30)])
async def test_agent_tabs_preserve_drafts_and_route_actions(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
):
    state = {
        "ada": {
            "agent_id": "ada",
            "name": "Ada",
            "role": "Research",
            "thread": "ada-thread",
            "tasks": [{"id": "ada-task", "status": "completed", "text": "Ada history"}],
            "approvals": [{"id": "ada-approval", "task": "ada-task", "state": "pending"}],
        },
        "bo": {
            "agent_id": "bo",
            "name": "Bo",
            "role": "Writing",
            "thread": "bo-thread",
            "tasks": [{"id": "bo-task", "status": "completed", "text": "Bo history"}],
        },
    }
    commands = []

    async def request(action: str, payload: dict | None = None) -> dict:
        if action == "status":
            return {"agents": copy.deepcopy(list(state.values()))}
        commands.append((action, payload))
        if action == "create":
            state["cleo"] = {
                "role": "Assistant",
                **payload,
                "agent_id": "cleo",
                "thread": "cleo-thread",
                "tasks": [],
            }
            return copy.deepcopy(state["cleo"])
        if action == "delete":
            del state[payload["agent_id"]]
            return {"agent_id": payload["agent_id"]}
        return copy.deepcopy(state[payload["agent_id"]])

    monkeypatch.setattr(teammates, "request", request)
    app = App()
    async with app.run_test(size=size) as pilot:
        screen = teammates.TeammatesScreen()
        app.push_screen(screen)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        tabs = screen.query_one("#agent-tabs", TabbedContent)
        ada, bo = screen.panels["ada"], screen.panels["bo"]
        ada.query_one("#teammate-task", TextArea).load_text("Unsent Ada draft")
        bo.query_one("#teammate-task", TextArea).load_text("Unsent Bo draft")
        ada.query_one("#teammate-role", TextArea).load_text("Edited Ada role")
        await pilot.click(tabs.get_tab("agent-bo"))
        await pilot.pause()
        bo.query_one("#teammate-tabs", TabbedContent).active = "history-tab"
        await pilot.pause()
        assert bo.query_one("#history-task", Select).value == "bo-task"
        await pilot.click(tabs.get_tab("agent-ada"))
        await pilot.pause()
        await pilot.click(ada.query_one("#approve", Button))
        await app.workers.wait_for_complete()
        assert commands[-1] == (
            "decide",
            {"agent_id": "ada", "id": "ada-approval", "approve": True},
        )
        await pilot.click(tabs.get_tab("agent-bo"))
        await pilot.pause()
        assert bo.query_one("#teammate-tabs", TabbedContent).active == "history-tab"
        bo.query_one("#teammate-tabs", TabbedContent).active = "work-tab"
        await pilot.pause()
        assign = bo.query_one("#assign", Button)
        assign.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click(assign, offset=(2, 1)), (
            tabs.active,
            assign.region,
            assign.disabled,
            app.get_widget_at(*assign.region.center),
            bo.query_one("#teammate-tabs", TabbedContent).active,
        )
        await app.workers.wait_for_complete()
        assert commands[-1] == ("assign", {"agent_id": "bo", "text": "Unsent Bo draft"})
        assert ada.query_one("#teammate-task", TextArea).text == "Unsent Ada draft"
        assert ada.query_one("#teammate-role", TextArea).text == "Edited Ada role"
        await pilot.click(tabs.get_tab("agent-new"))
        await pilot.pause()
        new = tabs.get_pane("agent-new").query_one(teammates.TeammatePanel)
        new.query_one("#teammate-name", Input).value = "Cleo"
        create = new.query_one("#create", Button)
        create.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click(create)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert tabs.active == "agent-cleo"
        assert set(screen.panels) == {"ada", "bo", "cleo"}
        assert screen.panels["ada"] is ada
        assert tabs.get_pane("agent-new").query_one("#teammate-name", Input).value == ""


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
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert screen.panels, str(screen.query_one("#roster-error").content)
        panel = next(iter(screen.panels.values()), None) or screen.query_one(
            teammates.TeammatePanel
        )
        panel.query_one("#teammate-tabs", TabbedContent).active = "settings-tab"
        await pilot.pause()
        assert panel.query_one("#teammate-model", Input).value == "saved-custom-model"
        panel.query_one("#teammate-provider", Select).value = "anthropic"
        await pilot.pause()
        panel.query_one("#teammate-model", Input).value = "edited-custom-model"
        await screen.reload().wait()
        assert panel.query_one("#teammate-model", Input).value == "edited-custom-model"
        button = panel.query_one("#edit", Button)
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
        await app.workers.wait_for_complete()
        await pilot.pause()
        panel = next(iter(screen.panels.values()), None) or screen.query_one(
            teammates.TeammatePanel
        )
        await screen.reload().wait()
        assert panel.loaded, str(panel.query_one("#teammate-error").render())
        task_input = panel.query_one("#teammate-task", TextArea)
        task_input.load_text("Unsent task draft")
        panel.query_one("#teammate-role", TextArea).load_text("Edited role")
        await screen.reload().wait()
        assert task_input.text == "Unsent task draft"
        assert panel.query_one("#teammate-role", TextArea).text == "Edited role"
        approve = panel.query_one("#approve", Button)
        assert approve.region.bottom <= screen.query_one("#teammate-panel").region.bottom
        await pilot.click(approve)
        await pilot.pause()
        assert decisions == [{"id": "exact-proposal", "approve": True}]

        panel.query_one("#teammate-tabs", TabbedContent).active = "history-tab"
        await pilot.pause()
        selector = panel.query_one("#history-task", Select)
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
        await app.workers.wait_for_complete()
        await pilot.pause()
        panel = next(iter(screen.panels.values()), None) or screen.query_one(
            teammates.TeammatePanel
        )
        assert panel.query_one("#teammate-tabs", TabbedContent).active == "settings-tab"
        assert panel.query_one("#create", Button).display
        assert panel.query_one("#assign", Button).disabled
        panel.query_one("#teammate-name", Input).value = "Ada"
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
        await app.workers.wait_for_complete()
        await pilot.pause()
        panel = next(iter(screen.panels.values()), None) or screen.query_one(
            teammates.TeammatePanel
        )
        task = panel.query_one("#teammate-task", TextArea)
        assert task.styles.border_top[0] == "round"
        assert task.styles.background.a > 0
        assert task.content_region.height >= 3
        task.focus()
        await pilot.press("h", "i", "enter", "x")
        assert task.text == "hi\nx"
        tabs = panel.query_one("#teammate-tabs", TabbedContent)
        await pilot.click(tabs.get_tab("settings-tab"))
        await pilot.pause()
        name = panel.query_one("#teammate-name")
        role = panel.query_one("#teammate-role", TextArea)
        assert name.styles.border_top[0] == "round"
        assert name.region.height >= 3
        assert role.styles.border_top[0] == "round"
        assert role.content_region.height >= 3
        for field in ("#teammate-model", "#teammate-vision-model"):
            editor = panel.query_one(field, Input)
            assert editor.styles.border_top[0] == "round"
            assert editor.styles.height.value >= 3
        save = panel.query_one("#edit", Button)
        assert screen.query_one("#teammate-panel").region.contains_region(save.region)
        name.focus()
        await pilot.press("end", "z")
        assert name.value == "Adaz"
        role.focus()
        await pilot.press("z")
        assert role.text == "zAssistant"
