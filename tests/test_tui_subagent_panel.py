"""Local subagents render as plain chat status lines; remote tasks retain their panel."""

from __future__ import annotations

import asyncio
import time

import pytest

from novacode_cli import ui_events as ev

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


@pytest.fixture(autouse=True)
def _isolate_panel_tests(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    import novacode_cli.config.config as config_module
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr(config_module, "HOME_DIR", tmp_path / ".nova")

    def isolated_config(config):
        config.config_dir = tmp_path / ".nova"
        config.config_path = config.config_dir / "Nova.config.json"
        config._config = {}
        config._loaded = {}
        config._load()

    monkeypatch.setattr(NovaConfig, "__init__", isolated_config)
    if _HAS_TEXTUAL:
        from novacode_cli.tui.app import NovaApp

        async def no_warmup(self: object) -> None:
            pass

        monkeypatch.setattr(NovaApp, "_eager_voice_warmup", no_warmup)


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="deepseek-v4.1-flash",
    )


def _task(task_id: str, **kwargs) -> ev.SubagentTask:
    base: dict[str, object] = {
        "task_id": task_id,
        "status": "running",
        "label": "research: docling",
        "started_at": 0.0,
    }
    base.update(kwargs)
    return ev.SubagentTask(**base)  # type: ignore[arg-type]



@pytest.mark.parametrize("phase_kind", ["direct", "eval"])
async def test_local_task_is_inline_noninteractive_and_not_in_dock(phase_kind):
    from textual.widgets import Collapsible, Static

    app = _app()
    async with app.run_test(size=(120, 44)) as pilot:
        await app._render(_task("a", phase_kind=phase_kind, subagent_type="reviewer"))
        await pilot.pause()
        widget = app._inline_subagents["a"]
        assert isinstance(widget, Static)
        assert not isinstance(widget, Collapsible)
        assert not widget.can_focus
        assert "reviewer" in str(widget.render())
        assert "running" in str(widget.render())
        assert not app.query_one("#subagents-dock").has_class("active")
        assert app._subagents_tick is None
        screen = app.screen
        before = str(widget.render())
        await pilot.click(widget)
        await pilot.pause()
        assert app.screen is screen
        assert str(widget.render()) == before
        assert not app.query("Collapsible.subagent")
        await app._render(ev.SubagentPreview("a", "tool", "grep(secret)"))
        assert not app._subagent_previews
        assert len(app.query(".subagent-inline")) == 1


@pytest.mark.parametrize("status,word", [("done", "completed"), ("failed", "failed"), ("stopped", "stopped")])
async def test_inline_status_updates_same_line_and_keeps_other_agent_running(status, word):
    app = _app()
    async with app.run_test(size=(120, 44)):
        await app._render(_task("a", subagent_type="reviewer"))
        await app._render(_task("b", subagent_type="reviewer"))
        widget = app._inline_subagents["a"]
        await app._render(_task("a", status=status, duration_ms=2500,
                                error="provider unavailable" if status == "failed" else ""))
        assert app._inline_subagents["a"] is widget
        assert word in str(widget.render())
        assert "2.5s" in str(widget.render())
        if status == "failed":
            assert "provider unavailable" in str(widget.render())
        assert len(app.query(".subagent-inline")) == 2
        assert set(app._subagent_widgets) == {"b"}
        assert "running" in str(app._inline_subagents["b"].render())


async def test_activity_and_detailed_events_share_one_inline_line():
    app = _app()
    async with app.run_test(size=(120, 44)):
        await app._render(ev.SubagentActivity("dispatched", subagent_type="reviewer", call_id="a"))
        widget = app._inline_subagents["a"]
        await app._render(_task("a", phase_kind="direct", subagent_type="reviewer"))
        await app._render(ev.SubagentActivity("tool_start", call_id="a", message="grep(secret)", detail="tool"))
        await app._render(ev.SubagentActivity("status", call_id="a", message="verbose progress"))
        assert app._inline_subagents["a"] is widget
        assert len(app.query(".subagent-inline")) == 1
        assert "grep" not in str(widget.render())
        assert not app._subagent_tool_to_task
        await app._render(ev.SubagentActivity("completed", subagent_type="reviewer", call_id="a"))
        await app._render(_task("a", phase_kind="direct", status="failed", error="rate limit"))
        assert "failed" in str(widget.render())
        assert "rate limit" in str(widget.render())
        assert len(app.query(".subagent-inline")) == 1


@pytest.mark.parametrize("event", [ev.Done(), ev.Cancelled(), ev.Error("provider stopped")])
async def test_turn_end_marks_inline_tasks_stopped(event):
    app = _app()
    async with app.run_test(size=(120, 44)):
        await app._render(_task("a", started_at=time.time() - 3))
        await app._render(event)
        assert app._subagent_rows["a"].status == "stopped"
        assert "stopped" in str(app._inline_subagents["a"].render())
        assert not app._subagent_widgets
        assert app._subagents_tick is None


async def test_async_panel_survives_local_turn_and_local_tasks_stay_inline():
    app = _app()
    async with app.run_test(size=(120, 44)):
        app._ingest_async_tasks({"a": {"task_id": "a", "agent_name": "remote-reviewer", "status": "running"}})
        await app._render(_task("local", subagent_type="local-reviewer"))
        app._paint_subagents()
        assert {row.task_id for row in app._subagent_task_list()} == {"async:a"}
        body = str(app.query_one("#subagents-body").render())
        assert "remote-reviewer" in body
        assert "local-reviewer" not in body
        await app._render(ev.Cancelled())
        assert app._subagent_rows["async:a"].status == "running"
        assert app._subagents_tick is not None
        app._ingest_async_tasks({"a": {"task_id": "a", "status": "success"}})
        app._paint_subagents()
        assert not app.query_one("#subagents-dock").has_class("active")
        assert app._subagents_tick is None


async def test_clear_removes_inline_state(monkeypatch):
    app = _app()

    async def no_learning():
        pass

    monkeypatch.setattr(app, "_consolidate_learning", no_learning)
    async with app.run_test(size=(120, 44)) as pilot:
        await app._render(_task("a"))
        await app._run_clear()
        await pilot.pause()
        assert not app._inline_subagents
        assert not app._subagent_rows
        assert not app._subagent_widgets
        assert not app.query(".subagent-inline")


async def test_escape_cancels_stream_and_stops_inline_subagent(monkeypatch):
    app = _app()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stream(_text, _assistant_id=None):
        await app._render(_task("live", subagent_type="researcher", started_at=time.time()))
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(app, "_do_stream", stream)
    async with app.run_test(size=(120, 44)) as pilot:
        app.run_worker(app._stream_prompt("run a subagent"), group="turn")
        from tests.test_tui_app import _wait_until
        assert await _wait_until(pilot, started.is_set, timeout=5)
        await pilot.press("escape")
        assert await _wait_until(pilot, cancelled.is_set, timeout=5)
        assert await _wait_until(pilot, lambda: not app._turn_active)
        assert app._subagent_rows["live"].status == "stopped"
        assert "stopped" in str(app._inline_subagents["live"].render())
        assert app._subagents_tick is None


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_worker_cleanup_stops_rows_without_a_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    app = _app()

    async def interrupted_stream(_text: str, _assistant_id: str | None) -> None:
        await app._render(_task("unfinished", phase_kind="direct", started_at=time.time()))
        raise failure()

    monkeypatch.setattr(app, "_do_stream", interrupted_stream)
    async with app.run_test(size=(120, 44)) as pilot:
        turn = app._dispatch("start")
        await turn.wait()
        await pilot.pause()
        assert not app._turn_active
        assert app._subagent_rows["unfinished"].status == "stopped"
        assert app._subagent_rows["unfinished"].duration_ms is not None
        assert app._subagents_tick is None


async def test_spawned_teammate_uses_inline_lifecycle_without_agent_server(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from novacode_cli.teams.coordinator import TeamCoordinator
    from novacode_cli.teams.models import Identity

    server_specs = Mock(side_effect=AssertionError("Team display must not consult the agent server"))
    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents", server_specs
    )
    adapter = AsyncMock()
    adapter.start.return_value.stop.return_value = True
    host = TeamCoordinator(tmp_path, Identity("project", "root"), {"default": adapter})
    app = _app()
    try:
        team = await host.request(host.lead, "team_create", {"name": "review"})
        agent = await host.spawn(host.lead, "Review the code", team["id"])
        await host.on_event(agent["id"], "running", {"model": "configured-model"})
        events = host.drain_events()
        assert events and all(event.phase_kind == "team" for event in events)
        async with app.run_test(size=(120, 44)) as pilot:
            for event in events:
                await app._render(event)
            await pilot.pause()
            widget = app._inline_subagents[agent["id"]]
            assert "running" in str(widget.render())
            assert not app.query_one("#subagents-dock").has_class("active")
            await app._render(ev.Done())
            await app._render(ev.Cancelled())
            assert app._subagent_rows[agent["id"]].status == "running"
            assert agent["id"] in app._subagent_widgets
            assert "running" in str(widget.render())
            screen = app.screen
            await pilot.click(widget)
            app._open_subagent_preview(agent["id"])
            await pilot.pause()
            assert app.screen is screen
            server_specs.assert_not_called()
            await host.on_event(agent["id"], "idle", {"summary": "Review completed"})
            for event in host.drain_events():
                await app._render(event)
            assert app._inline_subagents[agent["id"]] is widget
            assert "completed" in str(widget.render())
            assert app._subagent_rows[agent["id"]].model == "configured-model"
            assert agent["id"] not in app._subagent_widgets
            server_specs.assert_not_called()
    finally:
        await host.close()
