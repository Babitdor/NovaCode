"""Readable session completions preserve exact routing and remote topic identity."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from textual.widgets import OptionList

from novacode_cli.sessions.messaging import MessageError, resolve_mention
from tests.test_tui_sessions import _add_pane, _app, isolated_session_config  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("name", ["API tests", 'Review "login"', "Prøject 🚀", r"API\worker"])
def test_quoted_session_name_routes_exactly(name: str):
    text = f"@tab:{json.dumps(name, ensure_ascii=False)} Explain login"
    assert resolve_mention(text, {"b": name}) == ("b", "Explain login")


def test_quoted_names_keep_ambiguity_and_missing_message_errors():
    assert resolve_mention('@"API tests" hello', {"b": "API tests"}) is None
    with pytest.raises(MessageError, match="Ambiguous"):
        resolve_mention('@tab:"API tests" hello', {"b": "API tests", "c": "API tests"})
    with pytest.raises(MessageError, match="message"):
        resolve_mention('@tab:"API tests"', {"b": "API tests"})
    with pytest.raises(MessageError):
        resolve_mention('@tab:"API tests hello', {"b": "API tests"})


@pytest.mark.parametrize("accept", ["tab", "enter", "click"])
async def test_completion_matches_project_and_inserts_only_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, accept: str
):
    app = _app()
    async with app.run_test() as pilot:
        pane = await _add_pane(app, sid="b", title="API tests")
        pane.worktree = tmp_path / "backend"
        monkeypatch.setattr(app, "_get_agent_names", list)
        expected = '@tab:"API tests"'
        assert app._palette_candidates("@tab:backend") == [expected]
        assert app._palette_candidates('@tab:"API te') == [expected]
        assert app._palette_candidates("@tab:b") == [expected]
        prompt = app.query_one("#prompt")
        prompt.value = "@tab:backend"
        prompt.cursor_position = len(prompt.value)
        await pilot.pause(0.2)
        palette = app.query_one("#cmdpalette", OptionList)
        assert palette.display
        option = palette.get_option_at_index(0)
        assert "backend · API tests" in str(option.prompt)
        assert option.id == expected
        if accept == "click":
            app.on_option_list_option_selected(SimpleNamespace(option_list=palette, option=option))
        else:
            await pilot.press(accept)
        assert prompt.value == expected + " "
        assert not app._turn_active
        assert app._resolve_tab_mention(prompt.value + "Explain login") == ("b", "Explain login")


async def test_duplicate_names_use_ids_and_exited_tabs_are_excluded(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _app()
    async with app.run_test():
        await _add_pane(app, sid="b", title="API tests")
        other = await _add_pane(app, sid="c", title="API tests")
        monkeypatch.setattr(app, "_get_agent_names", list)
        assert app._palette_candidates("@tab:API") == ["@tab:b", "@tab:c"]
        other.status = "exited"
        assert app._palette_candidates("@tab:API") == ['@tab:"API tests"']


@pytest.mark.parametrize("platform", ["telegram", "discord"])
async def test_remote_topic_receives_project_and_name_without_changing_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
):
    from novacode_cli.tui.app import NovaApp

    app = _app()
    async with app.run_test():
        pane = await _add_pane(app, sid="b", title="API tests")
        pane.worktree = tmp_path / "backend"
        pane.state["session_state"] = SimpleNamespace(session_id="durable-b")
        bridge = SimpleNamespace(
            _config=SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id=7),
            ensure_session_topic=AsyncMock(return_value=55),
            post=AsyncMock(),
        )
        monkeypatch.setattr(NovaApp, "_remote_topic_bridges", lambda _self: [bridge])
        await app._open_remote_topics(pane, attach_running=False)
        bridge.ensure_session_topic.assert_awaited_once_with(
            "b", "durable-b", "backend · API tests"
        )
        assert app._remote_router.topic_of(7, "b") == 55
        assert "backend · API tests" in bridge.post.await_args.args[0]
        await app._open_remote_topics(pane, attach_running=False)
        bridge.post.assert_awaited_once()


async def test_root_session_has_readable_project_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from novacode_cli.config.config import settings

    project = tmp_path / "frontend"
    project.mkdir()
    monkeypatch.setattr(settings, "get_workspace_root", lambda: project)
    app = _app()
    async with app.run_test():
        assert app._root_pane.title == "Main"
        assert app._root_pane.display_name == "frontend · Main"
        assert app._root_pane.worktree is None
        app.session_state.session_id = "another-id"
        app._refresh_session_identity()
        assert app._root_pane.display_name == "frontend · Main"
