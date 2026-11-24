"""The Background tasks summary line and the down-arrow gesture.

Collapsed shape (coding-CLI style):

    Background
    1 active shell  (↓ tasks)

Down on the last composer line — after input history is exhausted — opens the
Background Tasks panel (the expanded list from the report). Ctrl+B with an
empty prompt, a click on the bar, and `/tasks` open the same panel.
"""

from __future__ import annotations

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")

from novacode_cli.tui.screens import BackgroundTasksScreen  # noqa: E402


@pytest.fixture(autouse=True)
def _offline_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    import novacode_cli.config.config as config_module

    monkeypatch.setattr(config_module, "HOME_DIR", tmp_path / ".nova")
    from novacode_cli.config.nova_config import NovaConfig

    def isolated_config(config):
        config.config_dir = tmp_path / ".nova"
        config.config_path = config.config_dir / "Nova.config.json"
        config._config = {}
        config._loaded = {}
        config._load()

    monkeypatch.setattr(NovaConfig, "__init__", isolated_config)


class _FakeAgent:
    async def aget_state(self, config):
        class _State:
            values = {"messages": []}

        return _State()


class _SS:
    thread_id = "t1"
    session_id = "s1"
    auto_approve = False
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


def _app(home_style: str = "minimal"):
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )
    app._home_style = home_style
    return app


def _fake_job(command: str = "uv run pytest -q"):
    import time

    from novacode_cli.shell.jobs import BackgroundJob

    return BackgroundJob(
        id=1,
        command=command,
        tool_name="tui-shell",
        started_at=time.time(),
        status="running",
    )


async def test_tasks_bar_renders_background_header_and_count(monkeypatch) -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        monkeypatch.setattr(app._tasks_registry(), "active", lambda: [_fake_job()])
        app._refresh_tasks_bar()
        await pilot.pause()
        bar = app.query_one("#tasks-bar")
        assert bar.has_class("active")
        rendered = str(bar.render())
        assert "Background" in rendered
        assert "1 active shell" in rendered
        assert "↓ tasks" in rendered


async def test_bar_hides_when_nothing_runs(monkeypatch) -> None:
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        monkeypatch.setattr(app._tasks_registry(), "active", lambda: [])
        app._refresh_tasks_bar()
        await pilot.pause()
        assert not app.query_one("#tasks-bar").has_class("active")


async def test_down_arrow_opens_the_background_tasks_panel(monkeypatch) -> None:
    app = _app()
    opened: list[bool] = []

    async def fake_panel():
        opened.append(True)
        return None

    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        monkeypatch.setattr(app._tasks_registry(), "active", lambda: [_fake_job()])
        app._refresh_tasks_bar()
        await pilot.pause()

        # Mimic the real @work contract: calling _open_tasks_panel spawns the
        # work; the caller never awaits it.
        monkeypatch.setattr(
            app,
            "_open_tasks_panel",
            lambda: app.run_worker(fake_panel(), exclusive=False),
        )
        # First down: input history is empty so the recall misses → the
        # gesture falls through to the tasks panel.
        await pilot.press("down")
        await pilot.pause(0.3)
        assert opened, "down on the last line must open the background tasks"


async def test_down_without_active_tasks_keeps_history_behavior(monkeypatch) -> None:
    app = _app()
    opened: list[bool] = []

    async def fake_panel():
        opened.append(True)
        return None

    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        monkeypatch.setattr(app._tasks_registry(), "active", lambda: [])
        app._refresh_tasks_bar()
        await pilot.pause()
        monkeypatch.setattr(
            app,
            "_open_tasks_panel",
            lambda: app.run_worker(fake_panel(), exclusive=False),
        )

        prompt = app.query_one("#prompt")
        prompt.text = "typed text"
        prompt.move_cursor(prompt.document.end)
        await pilot.press("down")
        await pilot.pause(0.3)
        assert not opened, "no active tasks: down must not open the panel"


async def test_open_background_tasks_runs_the_real_at_work_panel(monkeypatch) -> None:
    """open_background_tasks → _open_tasks_panel with its @work decorator intact.

    v1.2.58 wrapped the call in a second run_worker; since @work already
    returns a Worker (not a coroutine), that raised
    "Unsupported attempt to run an async worker" on first real use.
    """
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._home_style = "minimal"
        app._refresh_info_bar()
        monkeypatch.setattr(app._tasks_registry(), "active", lambda: [_fake_job()])
        app._refresh_tasks_bar()
        await pilot.pause()

        app.open_background_tasks()
        await pilot.pause(0.3)
        assert any(
            isinstance(screen, BackgroundTasksScreen) for screen in app.screen_stack
        ), "the real @work panel must open without a WorkerError"
        app.exit(None)
