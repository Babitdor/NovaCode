"""/settings: agent, tool-output and learning controls; appearance lives in /theme."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.app import App
from textual.widgets import Select, Switch

import novacode_cli.onboarding  # noqa: F401 - loaded before the TUI, as at startup
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.tui.screens import SettingsScreen

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.config.credentials.credential_value", lambda _var: "")


class Host(App):
    """Records what the settings ask the app to do."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def _rebuild_agent(self, saved: str) -> None:
        self.calls.append(f"rebuild: {saved}")

    async def _run_effort(self, text: str) -> None:
        self.calls.append(text)

    async def _run_learning(self, text: str) -> None:
        self.calls.append(text)

    def _set_animation_fps(self, _fps: int) -> None:
        pass


async def _open(app: Host, pilot):  # noqa: ANN001, ANN202
    screen = SettingsScreen()
    app.push_screen(screen)
    await pilot.pause()
    return screen


async def _settle(pilot, app: Host, count: int) -> None:  # noqa: ANN001
    for _ in range(60):
        await pilot.pause()
        if len(app.calls) >= count:
            return


async def test_appearance_moved_out_and_new_sections_are_in():
    app = Host()
    async with app.run_test(size=(100, 60)) as pilot:
        screen = await _open(app, pilot)
        assert not screen.query("#home-style-select")
        assert not screen.query("#matrix-rain-toggle")
        for widget in ("#effort-select", "#context-budget-select", "#decision-pruning-toggle",
                       "#learning-toggle", "#learning-auto-toggle", "#settings-theme"):
            assert screen.query_one(widget)


async def test_decision_pruning_needs_its_models_key(monkeypatch):
    NovaConfig().set_tool_verdict_settings(
        enabled=False, endpoint=NovaConfig.TOOL_VERDICT_JEV_ENDPOINT, model="jev-latest"
    )
    app = Host()
    async with app.run_test(size=(100, 60)) as pilot:
        screen = await _open(app, pilot)
        switch = screen.query_one("#decision-pruning-toggle", Switch)
        switch.value = True
        for _ in range(30):
            await pilot.pause()
        assert switch.value is False, "switched back: there is no Jev key"
        assert NovaConfig().get_tool_verdicts_enabled() is False
        assert app.calls == []

        monkeypatch.setattr(
            "novacode_cli.config.credentials.credential_value",
            lambda var: "k" if var == "TYPESAFE_API_KEY" else "",
        )
        switch.value = True
        await _settle(pilot, app, 1)
        assert NovaConfig().get_tool_verdicts_enabled() is True
        assert app.calls == ["rebuild: Decision-based tool pruning on (jev-latest)."]


async def test_effort_budget_and_learning_apply_through_their_commands():
    app = Host()
    async with app.run_test(size=(100, 60)) as pilot:
        screen = await _open(app, pilot)
        screen.query_one("#effort-select", Select).value = "high"
        await _settle(pilot, app, 1)
        screen.query_one("#context-budget-select", Select).value = 400_000
        await _settle(pilot, app, 2)
        screen.query_one("#learning-auto-toggle", Switch).value = True
        await _settle(pilot, app, 3)
        assert app.calls == [
            "/effort high",
            "rebuild: Context budget: 400K.",
            "/learning auto on",
        ]
        assert NovaConfig().get("context_budget_tokens") == 400_000


async def test_a_hand_set_budget_stays_selectable():
    NovaConfig().set("context_budget_tokens", 150_000)
    app = Host()
    async with app.run_test(size=(100, 60)) as pilot:
        screen = await _open(app, pilot)
        assert screen.query_one("#context-budget-select", Select).value == 150_000
        assert app.calls == []
