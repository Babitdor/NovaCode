"""Command coverage, filters and responsive reference-panel layouts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.app import App
from textual.widgets import Input, OptionList, Select, Static

from novacode_cli.tui.app import _LIVE_UI_COMMANDS, TUI_COMMANDS, NovaApp
from novacode_cli.tui.reference_screens import EvolutionScreen, HelpScreen, help_entries


class Host(App):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def on_mount(self):
        self.push_screen(self.panel)


def test_help_covers_commands_aliases_and_runtime_extensions():
    assert {"help", "evolution"} <= _LIVE_UI_COMMANDS
    entries = help_entries(
        TUI_COMMANDS,
        plugins=("review-code", "help"),
        panel_commands=("my-panel",),
        skills=("release-notes", "help"),
    )
    commands = [entry.command for entry in entries]
    assert len(commands) == len(set(commands))
    assert all(f"/{name}" in commands for name in TUI_COMMANDS)
    assert {"/review-code", "/my-panel", "/release-notes"} <= set(commands)
    for name, spec in TUI_COMMANDS.items():
        entry = next(e for e in entries if e.command == f"/{name}")
        assert entry.description == spec.help
        assert entry.aliases == tuple(f"/{alias}" for alias in spec.aliases)
    assert "/themes" in NovaApp._help_text(None).plain


@pytest.mark.asyncio
async def test_help_searches_aliases_and_skills_without_mounting_large_lists():
    panel = HelpScreen(
        TUI_COMMANDS,
        plugins=("plugin-demo",),
        skill_loader=lambda: [f"skill-{i}" for i in range(1200)],
    )
    async with Host(panel).run_test(size=(100, 40)) as pilot:
        await panel.workers.wait_for_complete()
        assert not any(e.category == "Skills" for e in panel.visible_entries)
        search = panel.query_one(Input)
        search.value = "/themes"
        await pilot.pause()
        assert [e.command for e in panel.visible_entries] == ["/theme"]
        search.value = "skill-1199"
        await pilot.pause()
        assert [e.command for e in panel.visible_entries] == ["/skill-1199"]
        search.value = ""
        panel.query_one(Select).value = "Skills"
        await pilot.pause()
        assert panel.query_one(OptionList).option_count == 200
        search.value = "no-such-command"
        await pilot.pause()
        assert panel.query_one(OptionList).option_count == 0
        assert "No matching commands" in str(panel.query_one("#reference-detail", Static).content)
        await pilot.press("escape")
        assert panel.app.screen is not panel


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(40, 24), (80, 30), (120, 42)])
@pytest.mark.parametrize("kind", ["help", "evolution"])
async def test_reference_panels_fit_small_terminals(monkeypatch, size, kind):
    loader = AsyncMock(
        return_value=(
            {"unlocked": 3, "leveled": 8},
            [
                {
                    "kind": "unlock",
                    "skill": "[red]literal-skill",
                    "ts": 1720000000,
                    "task_summary": "A long summary " * 100,
                },
                {
                    "kind": "levelup",
                    "skill": "debugger",
                    "ts": 1720001000,
                    "task_summary": "Fixed auth",
                },
            ],
        )
    )
    monkeypatch.setattr("novacode_cli.commands.evolution_handler._load_evolution", loader)
    panel = HelpScreen(TUI_COMMANDS) if kind == "help" else EvolutionScreen()
    async with Host(panel).run_test(size=size) as pilot:
        await panel.workers.wait_for_complete()
        await pilot.pause()
        listing = panel.query_one(OptionList)
        close = panel.query_one("#reference-close")
        assert listing.region.height >= 3
        assert listing.region.right <= size[0]
        assert close.region.bottom <= size[1]
        assert close.region.y >= listing.region.bottom
        if kind == "evolution":
            panel.query_one(Select).value = "levelup"
            await pilot.pause()
            assert [e["skill"] for e in panel.visible_entries] == ["debugger"]
            assert "Fixed auth" in str(panel.query_one("#reference-detail", Static).content)


@pytest.mark.asyncio
async def test_evolution_empty_error_and_refresh(monkeypatch):
    loader = AsyncMock(
        side_effect=[RuntimeError("Store offline"), ({"unlocked": 0, "leveled": 0}, [])]
    )
    monkeypatch.setattr("novacode_cli.commands.evolution_handler._load_evolution", loader)
    panel = EvolutionScreen()
    async with Host(panel).run_test(size=(100, 40)) as pilot:
        await panel.workers.wait_for_complete()
        assert "Store offline" in str(panel.query_one("#reference-detail", Static).content)
        await pilot.click("#evolution-refresh")
        await panel.workers.wait_for_complete()
        assert "Your evolution starts" in str(panel.query_one("#reference-detail", Static).content)
        assert not panel.query_one("#evolution-refresh").disabled


@pytest.mark.asyncio
async def test_evolution_loader_uses_store_and_reports_failures(monkeypatch):
    from novacode_cli.commands.evolution_handler import _load_evolution

    store = SimpleNamespace(
        aget=AsyncMock(return_value=SimpleNamespace(value={"unlocked": 2, "leveled": 4})),
        asearch=AsyncMock(
            return_value=[SimpleNamespace(value={"ts": 1}), SimpleNamespace(value={"ts": 2})]
        ),
    )
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    counters, events = await _load_evolution(strict=True)
    assert counters == {"unlocked": 2, "leveled": 4}
    assert [e["ts"] for e in events] == [2, 1]
    store.asearch.side_effect = RuntimeError("Offline")
    with pytest.raises(RuntimeError, match="Offline"):
        await _load_evolution(strict=True)
    assert (await _load_evolution())[1] == []


@pytest.mark.asyncio
async def test_closing_evolution_cancels_the_pending_load(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_load(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("novacode_cli.commands.evolution_handler._load_evolution", blocked_load)
    panel = EvolutionScreen()
    host = Host(panel)
    async with host.run_test(size=(100, 40)) as pilot:
        await started.wait()
        await pilot.press("escape")
        await pilot.pause()
        assert host.screen is not panel
        await asyncio.wait_for(cancelled.wait(), timeout=2)
