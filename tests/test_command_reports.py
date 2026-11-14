"""Cache controls and diagnostic reports stay native, scoped and keyboard accessible."""

from __future__ import annotations

import asyncio
import json
from io import StringIO
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from rich.console import Console
from textual.app import App
from textual.widgets import Button, Static, Tree

from novacode_cli.commands.cache_handler import control_cache, handle_cache_command
from novacode_cli.tui.command_reports import CacheScreen, StructuredReportScreen
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

    from novacode_cli.tui.reference_screens import ReferenceScreen

pytestmark = pytest.mark.timeout(30)


class Host(App):
    def __init__(self, panel: ReferenceScreen) -> None:
        super().__init__()
        self.panel = panel

    def on_mount(self):
        self.push_screen(self.panel)


async def test_shared_cache_controller_tracks_workspace_and_assistant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    backend = SimpleNamespace(default=SimpleNamespace(cwd=first))
    state = SimpleNamespace(_backend=backend)
    assert not (await control_cache(state, "nova", "off"))["semantic_cache_enabled"]
    assert (await control_cache(state, "other-agent", "status"))["semantic_cache_enabled"]
    backend.default.cwd = second
    assert (await control_cache(state, "nova", "status"))["semantic_cache_enabled"]
    backend.default.cwd = first
    output = StringIO()
    monkeypatch.setattr("novacode_cli.commands.cache_handler.console", Console(file=output))
    await handle_cache_command(
        SimpleNamespace(
            session_state=state,
            assistant_id="nova",
            sandbox_type=None,
            cmd_args="status",
        )
    )
    assert json.loads(output.getvalue())["semantic_cache_enabled"] is False
    assert "local main-agent" in (await control_cache(state, "nova", "clear", "docker"))["error"]
    assert "Usage" in (await control_cache(state, "nova", "invalid"))["error"]


@pytest.mark.parametrize("size", [(40, 24), (100, 34)])
async def test_cache_metrics_and_buttons_use_authoritative_state(
    tmp_path: Path,
    size: tuple[int, int],
):
    state = SimpleNamespace(_backend=SimpleNamespace(default=SimpleNamespace(cwd=tmp_path)))

    async def control(action: str) -> dict:
        return await control_cache(state, "nova", action)

    panel = CacheScreen(control)
    async with Host(panel).run_test(size=size) as pilot:
        await panel.workers.wait_for_complete()
        await pilot.pause()
        assert str(panel.query_one("#cache-status", Static).content) == "Enabled"
        activity = str(panel.query_one("#cache-activity", Static).content)
        assert "No knowledge lookups yet" in activity
        assert "not file reads or shell commands" in activity
        output = StringIO()
        Console(file=output, width=38).print(panel.query_one("#cache-metrics", Static).content)
        assert "Exact hits" in output.getvalue()
        assert "Tokens avoided (est.)" in output.getvalue()
        assert "semantic_cache_enabled" not in output.getvalue()
        for button in panel.query(Button):
            assert button.region.x >= 0
            assert button.region.right <= size[0]
            assert button.region.bottom <= size[1]
        toggle = panel.query_one("#cache-toggle", Button)
        # Back-to-back synthetic presses must not hit the visual click debounce.
        toggle.active_effect_duration = 0
        toggle.focus()
        await pilot.press("enter")
        await panel.workers.wait_for_complete()
        await pilot.pause()
        assert str(panel.query_one("#cache-status", Static).content) == "Disabled"
        assert not (await control("status"))["semantic_cache_enabled"]
        assert await pilot.click("#cache-toggle", offset=(2, 1))
        await panel.workers.wait_for_complete()
        await pilot.pause()
        assert (await control("status"))["semantic_cache_enabled"]
        epoch = (await control("status"))["epoch"]
        assert await pilot.click("#cache-clear", offset=(2, 1))
        await panel.workers.wait_for_complete()
        assert (await control("status"))["epoch"] == epoch + 1
        await pilot.press("escape")
        assert panel.app.screen is not panel


async def test_cache_loading_prevents_overlapping_mutations_and_recovers_from_error():
    ready = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def control(action: str) -> dict:
        calls.append(action)
        ready.set()
        await release.wait()
        return {"error": "Cache database unavailable [literal]"}

    panel = CacheScreen(control)
    async with Host(panel).run_test() as pilot:
        await ready.wait()
        assert panel.query_one("#cache-toggle", Button).disabled
        panel.load("clear")
        await pilot.pause()
        assert calls == ["status"]
        release.set()
        await panel.workers.wait_for_complete()
        assert "[literal]" in str(panel.query_one("#cache-error", Static).content)
        assert str(panel.query_one("#cache-status", Static).content) == "Unavailable"
        panel.control = AsyncMock(return_value={"semantic_cache_enabled": False})
        await pilot.click("#cache-refresh")
        await panel.workers.wait_for_complete()
        assert str(panel.query_one("#cache-status", Static).content) == "Disabled"
        assert not str(panel.query_one("#cache-error", Static).content)


@pytest.mark.parametrize("size", [(40, 24), (100, 34)])
async def test_reports_keep_full_literal_values_and_allow_keyboard_close(size: tuple[int, int]):
    value = "literal [bold] data\n" + "long field " * 200
    data = {"history_tokens": 4200, "tools": [{"source": value}], "empty": []}
    panel = StructuredReportScreen("Session audit", data)
    async with Host(panel).run_test(size=size) as pilot:
        tree = panel.query_one(Tree)
        node = tree.root.children[1].children[0].children[0]
        tree.select_node(node)
        await pilot.pause()
        assert str(panel.query_one("#reference-detail", Static).content) == value
        assert node.label.plain.startswith("Source: literal [bold] data")
        assert panel.query_one("#reference-close", Button).region.bottom <= size[1]
        await pilot.press("escape")
        assert panel.app.screen is not panel


async def test_slash_routes_open_native_screens_without_polluting_chat(
    monkeypatch: pytest.MonkeyPatch,
):
    from tests.test_tui_home_preferences import _app

    app = _app()
    monkeypatch.setattr(app, "_passthrough_command", AsyncMock())
    monkeypatch.setattr(app, "_log", lambda *_args, **_kwargs: pytest.fail("Raw report in chat"))
    control = AsyncMock(return_value={"semantic_cache_enabled": True})
    monkeypatch.setattr("novacode_cli.commands.cache_handler.control_cache", control)
    monkeypatch.setattr(app, "_read_state", AsyncMock(return_value=SimpleNamespace(values={})))
    async with app.run_test(size=(90, 34)) as pilot:
        await pilot.pause()
        await app._run_slash("/cache")
        await app.screen.workers.wait_for_complete()
        assert isinstance(app.screen, CacheScreen)
        await pilot.press("escape")
        await app._run_slash("/ui inspect")
        assert isinstance(app.screen, StructuredReportScreen)
        await pilot.press("escape")
        await app._run_slash("/ui reset")
        assert not isinstance(app.screen, StructuredReportScreen)
        await app._run_slash("/lite-mode audit")
        assert isinstance(app.screen, StructuredReportScreen)
        await pilot.press("escape")
        app._passthrough_command.assert_not_awaited()
        control.assert_awaited_once()
