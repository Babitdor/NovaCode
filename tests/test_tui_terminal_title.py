"""Terminal titles follow the visible conversation and contain safe controls."""

from types import SimpleNamespace

from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401


async def test_terminal_title_tracks_visible_session_and_identity_changes():
    app = _app()
    async with app.run_test():
        assert app.title == f"NovaCode · {app.session_state.session_id[:8]}"
        child = await _add_pane(app, title="Fix authentication")
        await app._switch_to(child)
        assert app.title == "NovaCode · Fix authentication"
        child.title = "Review tests"
        app._refresh_tabs()
        assert app.title == "NovaCode · Review tests"
        await app._switch_to(app._root_pane)
        app.session_state.session_id = "resumed-session-id"
        app._refresh_session_identity()
        assert app._root_pane.title == "resumed-"
        assert app.title == "NovaCode · resumed-"
        app.session_state.session_id = "fresh-session-id"
        app._refresh_session_identity()
        assert app._root_pane.title == "fresh-se"
        assert app.title == "NovaCode · fresh-se"
        assert not getattr(app, "_terminal_title", None), (
            "headless mode must not write terminal controls"
        )


def test_terminal_driver_gets_one_safe_title_per_change():
    app = _app()
    writes = []
    flushes = []
    app._driver = SimpleNamespace(
        is_headless=False, write=writes.append, flush=lambda: flushes.append(True)
    )
    app._active_pane = SimpleNamespace(title="Fix\x1b\x07\x9c\n authentication 🦉")
    app._refresh_terminal_title()
    app._refresh_terminal_title()
    assert writes == ["\x1b]0;NovaCode · Fix authentication 🦉\x07"]
    assert flushes == [True]
    app._active_pane.title = " " * 500
    app._refresh_terminal_title()
    assert writes[-1] == "\x1b]0;NovaCode · main\x07"
    app._active_pane.title = "x" * 10000
    app._refresh_terminal_title()
    assert writes[-1] == "\x1b]0;NovaCode · " + "x" * 120 + "\x07"


def test_terminal_title_spinner_advances_only_while_working(monkeypatch):
    app = _app()
    writes = []
    app._driver = SimpleNamespace(is_headless=False, write=writes.append, flush=lambda: None)
    app._active_pane = SimpleNamespace(title="Add SendMessage tool", status="running", kind="child")
    monkeypatch.setattr("novacode_cli.tui.app.time.monotonic", lambda: 0)
    app._refresh_terminal_title()
    assert app.title == "◐ NovaCode · Add SendMessage tool"
    app._refresh_terminal_title()
    assert len(writes) == 1
    monkeypatch.setattr("novacode_cli.tui.app.time.monotonic", lambda: 0.25)
    app._refresh_terminal_title()
    assert app.title == "◓ NovaCode · Add SendMessage tool"
    assert len(writes) == 2
    app._active_pane.status = "idle"
    app._refresh_terminal_title()
    assert app.title == "NovaCode · Add SendMessage tool"
    assert len(writes) == 3
    app._active_pane.kind = "root"
    app._active_pane.status = "waiting"
    app._turn_active = True
    app._refresh_terminal_title()
    assert app.title == "NovaCode · Add SendMessage tool"
    app._active_pane.status = "starting"
    app._refresh_terminal_title()
    assert app.title.startswith("◓ ")


async def test_single_session_title_animation_starts_and_stops_with_work():
    app = _app()
    async with app.run_test():
        assert app._tab_refresh_timer is None
        app._turn_active = True
        app._refresh_tabs()
        assert app._tab_refresh_period == 0.25
        assert app._tab_refresh_timer is not None
        assert app.title[0] in "◐◓◑◒"
        app._turn_active = False
        app._refresh_tabs()
        assert app._tab_refresh_timer is None
        assert app._tab_refresh_period is None
        assert app.title.startswith("NovaCode · ")
