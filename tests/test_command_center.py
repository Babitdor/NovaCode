"""Command Center navigation, data boundaries and existing resume controls."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from textual.app import App
from textual.widgets import Input, OptionList, Static

from novacode_cli.session.session_persistence import SessionMeta
from novacode_cli.tui.command_center import (
    CommandCenterScreen,
    CommandSession,
    live_sessions,
    merge_sessions,
    message_preview,
    session_age,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


pytestmark = pytest.mark.timeout(60)


def meta(identity: str, *, project: str = "/project", status: str = "active") -> SessionMeta:
    return SessionMeta(
        session_id=identity,
        thread_id=f"thread-{identity}",
        created_at="2026-01-01T00:00:00+00:00",
        last_active="2026-11-01T00:00:00+00:00",
        project_root=project,
        repo_hash=None,
        Nova_md_checksum=None,
        model_name="saved-model",
        assistant_id="nova-agent",
        current_task=f"Task {identity}",
        task_status=status,
    )


def test_live_evidence_overrides_saved_status_without_duplicate_sessions():
    saved = [meta("active"), meta("blocked", status="blocked"), meta("old")]
    saved[1].blocked_reason = "Review the test result"
    rows = merge_sessions(
        saved,
        [
            CommandSession(
                key="session:active", session_id="active", pane_id="root", status="Working"
            )
        ],
    )
    assert len(rows) == 3
    assert rows[0].status == "Working"
    assert rows[0].model == "saved-model"
    assert rows[0].title == "Task active"
    assert {row.session_id: row.status for row in rows} == {
        "active": "Working",
        "blocked": "Needs you",
        "old": "Inactive",
    }
    saved[2].cleared = True
    assert "old" not in {row.session_id for row in merge_sessions(saved, [])}
    assert merge_sessions([meta("../outside"), meta("C:\\outside")], []) == []


def test_live_snapshot_keeps_hidden_panes_separate_and_never_reads_disk():
    root_state = SimpleNamespace(
        session_id="root-saved",
        thread_id="root-thread",
        workspace_root="/root",
        pending_approval_count=lambda: 0,
    )
    child_state = SimpleNamespace(session_id="child-saved", pending_approval_count=lambda: 0)
    root = SimpleNamespace(
        sid="root",
        title="Main",
        kind="root",
        status="idle",
        pending_interrupt=None,
        project_root="/root",
        worktree=None,
        branch=None,
        remote_prompt=None,
        state={},
    )
    child = SimpleNamespace(
        sid="child",
        title="Child task",
        kind="child",
        status="needs-approval",
        pending_interrupt={},
        project_root=None,
        worktree="/child",
        branch="nova/child",
        remote_prompt="Child prompt",
        state={
            "session_state": child_state,
            "model_name": "child-model",
            "_accumulated_reply": "Child response",
        },
    )
    app = SimpleNamespace(
        _panes=[root, child],
        _active_pane=root,
        session_state=root_state,
        _turn_active=True,
        model_name="root-model",
        _last_user_prompt="Root prompt",
        _last_user_prompt_thread="root-thread",
        _status_branch="main",
        _accumulated_reply="Root response",
        _reasoning_effort="medium",
        token_tracker=SimpleNamespace(
            has_api_data=True, session_input_tokens=120, session_total_tokens=150
        ),
    )
    rows = live_sessions(app)
    assert [(row.project, row.status, row.model) for row in rows] == [
        ("/root", "Working", "root-model"),
        ("/child", "Needs you", "child-model"),
    ]
    assert rows[0].input_tokens == 120
    assert rows[0].output_tokens == 30
    assert rows[1].input_tokens is None
    assert rows[1].last_message == "Child response"
    assert rows[1].branch == "nova/child"
    root_state.thread_id = "replacement-thread"
    assert live_sessions(app)[0].prompt != "Root prompt"


def test_previews_exclude_tools_and_reasoning_and_dates_fail_gracefully():
    prompt, reply = message_preview(
        [
            HumanMessage(content="Actual prompt"),
            ToolMessage(content="PRIVATE TOOL RESULT", tool_call_id="t"),
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "PRIVATE REASONING"},
                    {"type": "text", "text": "Visible answer"},
                ]
            ),
        ]
    )
    assert (prompt, reply) == ("Actual prompt", "Visible answer")
    assert session_age("") == "Live"
    assert session_age("invalid date") == "Unknown"
    assert session_age(datetime.now(UTC).isoformat()).endswith("s ago")


class Host(App):
    def __init__(self, screen: CommandCenterScreen) -> None:
        super().__init__()
        self.center = screen
        self.result = None

    def on_mount(self):
        self.push_screen(self.center, self.finish)

    def finish(self, result: CommandSession | None):
        self.result = result


class Manager:
    def __init__(self, rows: Sequence[SessionMeta]) -> None:
        self.rows = list(rows)
        self.threads = []
        self.previews = []

    def list_sessions(self, *, limit: int):
        self.threads.append(threading.get_ident())
        return self.rows[:limit]

    def load_recent_messages(self, session_id: str):
        self.threads.append(threading.get_ident())
        self.previews.append(session_id)
        return [HumanMessage(content="Saved prompt"), AIMessage(content="Saved answer")]


@pytest.mark.usefixtures("no_blocking_io")
async def test_project_filters_search_selection_and_open_use_stable_identity():
    manager = Manager(
        [meta("old", project="/alpha"), meta("blocked", project="/beta", status="blocked")]
    )
    current = CommandSession(
        key="session:live",
        session_id="live",
        pane_id="root",
        project="/alpha",
        title="Fix parser",
        status="Working",
    )
    screen = CommandCenterScreen(manager, lambda: [current])
    app = Host(screen)
    async with app.run_test(size=(140, 36)) as pilot:
        await screen.workers.wait_for_complete()
        await pilot.pause()
        listing = screen.query_one("#cc-list", OptionList)
        assert screen._selected().session_id == "live"
        assert threading.get_ident() not in manager.threads
        await pilot.press("tab")
        await pilot.pause()
        assert screen._selected().session_id == "blocked"
        assert "Needs you" in str(screen.query_one("#cc-detail-text", Static).render())
        await pilot.press("shift+tab", "ctrl+f")
        screen.query_one("#cc-search", Input).value = "old alpha"
        await pilot.pause()
        await screen.workers.wait_for_complete()
        assert screen._selected().session_id == "old"
        assert "Saved answer" in str(screen.query_one("#cc-detail-text", Static).render())
        await pilot.press("enter", "enter")
        await pilot.pause()
        assert app.result.session_id == "old"
        assert not app.result.pane_id
        assert listing.option_count >= 1


@pytest.mark.parametrize("size", [(80, 24), (60, 16), (140, 36)])
async def test_live_status_refresh_details_mode_and_escape(size: tuple[int, int]):
    rows = [
        CommandSession(
            key="pane:root", pane_id="root", title="Live task", status="Working", project="/project"
        )
    ]
    screen = CommandCenterScreen(None, lambda: rows)
    app = Host(screen)
    async with app.run_test(size=size) as pilot:
        await screen.workers.wait_for_complete()
        await pilot.pause()
        rows[0] = CommandSession(
            key="pane:root",
            pane_id="root",
            title="Live task",
            status="Needs you",
            project="/project",
        )
        screen._paint()
        await pilot.pause()
        assert screen._selected().status == "Needs you"
        await pilot.press("d")
        assert screen.has_class("cc-details-only")
        assert screen.query_one("#cc-details").display
        assert app.focused is screen.query_one("#cc-details")
        await pilot.press("escape")
        assert not screen.has_class("cc-details-only")
        await pilot.press("escape")
        assert app.result is None
        assert app.screen is not screen


async def test_metadata_error_retry_and_pagination_preserve_live_rows():
    manager = Manager([meta(str(index)) for index in range(28)])
    original = manager.list_sessions
    manager.list_sessions = Mock(side_effect=OSError("offline"))
    current = CommandSession(key="pane:root", pane_id="root", title="Live task", status="Ready")
    screen = CommandCenterScreen(manager, lambda: [current])
    app = Host(screen)
    async with app.run_test(size=(120, 32)) as pilot:
        await screen.workers.wait_for_complete()
        await pilot.pause()
        assert screen._selected().key == "pane:root"
        assert "retry" in str(screen.query_one("#cc-notice", Static).render())
        manager.list_sessions = original
        await pilot.press("r")
        await screen.workers.wait_for_complete()
        await pilot.pause()
        assert screen._has_more
        listing = screen.query_one("#cc-list", OptionList)
        listing.highlighted = listing.option_count - 1
        await pilot.press("enter")
        await screen.workers.wait_for_complete()
        assert len(screen._saved) == 28
        assert not screen._has_more


async def test_current_project_is_selected_first_and_filter_empty_state_is_recoverable():
    current = CommandSession(
        key="pane:current",
        pane_id="current",
        current=True,
        title="Current work",
        project="/z-last-alphabetically",
        status="Working",
    )
    screen = CommandCenterScreen(Manager([meta("old", project="/a-first")]), lambda: [current])
    app = Host(screen)
    async with app.run_test(size=(100, 28)) as pilot:
        await screen.workers.wait_for_complete()
        await pilot.pause()
        assert screen._selected().key == "pane:current"
        await pilot.press("tab")
        assert screen._selected() is None
        assert screen.query_one("#cc-empty", Static).display
        await pilot.press("shift+tab")
        assert screen._selected().key == "pane:current"


async def test_rapid_navigation_never_replaces_selected_details_with_a_stale_preview():
    import asyncio

    started, release = threading.Event(), threading.Event()
    manager = Manager([meta("a"), meta("b")])

    def preview(session_id: str) -> list[AIMessage]:
        if session_id == "a":
            started.set()
            assert release.wait(5)
        return [AIMessage(content=f"Answer for {session_id}")]

    manager.load_recent_messages = preview
    screen = CommandCenterScreen(manager, list)
    app = Host(screen)
    try:
        async with app.run_test(size=(120, 30)) as pilot:
            assert await asyncio.to_thread(started.wait, 5)
            listing = screen.query_one("#cc-list", OptionList)
            listing.highlighted = next(
                index
                for index in range(listing.option_count)
                if listing.get_option_at_index(index).id == "session:b"
            )
            await pilot.pause()
            release.set()
            await screen.workers.wait_for_complete()
            await pilot.pause()
            detail = str(screen.query_one("#cc-detail-text", Static).render())
            assert "Answer for b" in detail
            assert "Answer for a" not in detail
    finally:
        release.set()


@pytest.mark.parametrize("opened", [True, False])
async def test_app_opens_live_tab_or_existing_resume_flow_without_replacing_chat(*, opened: bool):
    from novacode_cli.tui.app import NovaApp

    pane = SimpleNamespace(sid="child")
    selected = CommandSession(
        key="session:saved", session_id="saved", pane_id="child" if opened else ""
    )
    app = SimpleNamespace(
        session_manager=None,
        _panes=[pane],
        push_screen_wait=AsyncMock(return_value=selected),
        _saved_session_pane=Mock(return_value=None),
        _switch_to=AsyncMock(),
        _resume_tab=AsyncMock(),
    )
    await NovaApp._run_command_center(app)
    if opened:
        app._switch_to.assert_awaited_once_with(pane)
        app._resume_tab.assert_not_awaited()
    else:
        app._resume_tab.assert_awaited_once_with("saved")
        app._switch_to.assert_not_awaited()


def test_command_is_registered_and_available_during_a_turn():
    from novacode_cli.tui.app import _LIVE_UI_COMMANDS, _TUI_COMMAND_ALIASES, TUI_COMMANDS

    assert TUI_COMMANDS["command-center"].handler == "_run_command_center"
    assert "command-center" in _LIVE_UI_COMMANDS
    assert _TUI_COMMAND_ALIASES["cc"] == "command-center"


@pytest.mark.parametrize("command", ["/command-center", "/cc"])
async def test_child_tab_opens_the_center_instead_of_forwarding_a_prompt(command: str):
    from novacode_cli.tui.app import NovaApp

    app = SimpleNamespace(_run_command_center=AsyncMock(), _send_child_prompt=AsyncMock())
    await NovaApp._dispatch_to_child(app, SimpleNamespace(), command)
    app._run_command_center.assert_awaited_once()
    app._send_child_prompt.assert_not_awaited()


async def test_stale_saved_selection_switches_to_a_tab_opened_while_browsing():
    from novacode_cli.tui.app import NovaApp

    pane = SimpleNamespace(sid="new-tab")
    selected = CommandSession(key="session:saved", session_id="saved")
    app = SimpleNamespace(
        session_manager=None,
        _panes=[pane],
        push_screen_wait=AsyncMock(return_value=selected),
        _saved_session_pane=Mock(return_value=pane),
        _switch_to=AsyncMock(),
        _resume_tab=AsyncMock(),
    )
    await NovaApp._run_command_center(app)
    app._switch_to.assert_awaited_once_with(pane)
    app._resume_tab.assert_not_awaited()


async def test_cancelling_command_center_does_not_open_or_resume_any_session():
    from novacode_cli.tui.app import NovaApp

    app = SimpleNamespace(
        session_manager=None,
        _panes=[],
        push_screen_wait=AsyncMock(return_value=None),
        _switch_to=AsyncMock(),
        _resume_tab=AsyncMock(),
    )
    await NovaApp._run_command_center(app)
    app._switch_to.assert_not_awaited()
    app._resume_tab.assert_not_awaited()
