"""Exercise staged changes through the actual Textual shell and agent tools."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novacode_cli.tui.harness import UIHarness, UISpec
from tests.test_tui_subagent_panel import _app


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from novacode_cli.tui.app import NovaApp

    async def no_warmup(_self: object) -> None:
        pass

    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("NOVA_SAFE_UI", raising=False)
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", no_warmup)
    monkeypatch.setattr(
        "novacode_cli.tui.harness.profile_path", lambda _workspace: tmp_path / "ui.json"
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"panels": [{"id": "prompt", "type": "python"}]},
        {"panels": [{"id": "x", "type": "notes"}] * 2},
        {"bindings": {"ctrl+q": "quit"}},
        {"commands": {"clear": "reset"}},
        {"width": 999},
        {"css": "Screen { display: none; }"},
        {"panels": [{"id": "x", "type": "notes", "visible": "true"}]},
    ],
)
def test_invalid_layout_rejected(patch: dict):
    with pytest.raises(ValidationError):
        UISpec.model_validate(patch)


@pytest.mark.asyncio
async def test_preview_commit_rollback_preserve_shell(tmp_path: Path):
    from novacode_cli import ui_events as ev
    from novacode_cli.tools.ui_tools import ui_commit, ui_inspect, ui_patch, ui_preview, ui_rollback

    app = _app()
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        core = {
            key: app.query_one(f"#{key}")
            for key in ["transcript", "prompt", "panes", "question-dock"]
        }
        core["prompt"].value = "unsent draft"
        panel = {"id": "notes", "type": "notes", "text": "[literal]", "title": "Notes"}
        reply = json.loads(
            await ui_patch.ainvoke(
                {"patch": {"panels": [panel], "bindings": {"f6": "toggle:notes"}}}
            )
        )
        assert reply["applied"]["panels"] == []
        await ui_preview.ainvoke({})
        await pilot.pause()
        assert app.query_one("#harness-notes").display
        assert not app._ui_harness.path.exists()
        await pilot.press("f6")
        await pilot.pause()
        assert not app.query_one("#harness-notes").display
        await ui_rollback.ainvoke({})
        assert len(app.query("#harness-notes")) == 0
        await ui_patch.ainvoke(
            {"patch": {"panels": [panel, {"id": "activity", "type": "activity"}]}}
        )
        await ui_commit.ainvoke({})
        assert app._ui_harness.path.exists()
        await app._render(ev.ToolResult("done", call_id="call-1"))
        assert "call-1" in str(app.query_one(".harness-activity").render())
        inspected = json.loads(await ui_inspect.ainvoke({}))
        assert not inspected["preview_active"]
        assert all(app.query_one(f"#{key}") is widget for key, widget in core.items())
        assert core["prompt"].value == "unsent draft"
        restored = UIHarness(app, tmp_path, app._ui_harness.path)
        assert await restored.initialize() == ""
        assert len(restored.applied.panels) == 2
        await pilot.press("ctrl+shift+backspace")
        await pilot.pause()
        assert not app._ui_harness.path.exists()
        assert not app.query_one("#ui-harness-dock").display


@pytest.mark.asyncio
async def test_invalid_theme_and_profile_keep_default(monkeypatch: pytest.MonkeyPatch):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = app._ui_harness
        await runtime.execute("patch", {"theme": "unknown-theme"})
        with pytest.raises(ValueError, match="Unknown theme"):
            await runtime.execute("preview")
        assert runtime.applied == UISpec()
        runtime.path.write_text("not json", encoding="utf-8")
        assert "default shell" in await runtime.initialize()
        monkeypatch.setenv("NOVA_SAFE_UI", "1")
        assert "Safe UI" in await runtime.initialize()


@pytest.mark.asyncio
async def test_tools_headless_are_unavailable():
    from novacode_cli.tools.ui_tools import ui_preview

    assert "running local Nova TUI" in await ui_preview.ainvoke({})
