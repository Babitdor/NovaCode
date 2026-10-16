"""Learning review screen decisions are revision specific and stay staged."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from textual.app import App
from textual.widgets import Button, OptionList

from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.tui.screens import LearningScreen

if TYPE_CHECKING:
    from pathlib import Path


class LearningApp(App[None]):
    def __init__(self, agent_dir: Path, project: str) -> None:
        super().__init__()
        self.agent_dir = agent_dir
        self.project = project


@pytest.mark.asyncio
async def test_learning_screen_approves_and_rejects_without_activating(tmp_path: Path) -> None:
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    first = ledger.propose(content="First checked lesson", project="project")
    second = ledger.propose(content="Second checked lesson", project="project")
    app = LearningApp(tmp_path, "project")
    async with app.run_test() as pilot:
        await app.push_screen(LearningScreen(tmp_path, "project"))
        await pilot.pause()
        assert app._exception is None, repr(app._exception)
        assert isinstance(app.screen, LearningScreen), repr(app.screen)
        screen = app.screen
        options = screen.query_one("#learning-list", OptionList)
        assert options.option_count == 2
        assert not screen.query_one("#learning-approve", Button).disabled
        await pilot.click("#learning-approve")
        await pilot.pause()
        assert ledger.history(first.id)[-1].status == "approved"
        assert ledger.list(project="project", active=True) == []
        options.highlighted = next(
            index for index, record in enumerate(screen._records) if record.id == second.id
        )
        await pilot.pause()
        assert screen._selected().id == second.id
        assert not screen.query_one("#learning-reject", Button).disabled
        await pilot.click("#learning-reject")
        await pilot.pause()
        assert ledger.history(second.id)[-1].status == "rejected"
        assert screen.query_one("#learning-reject", Button).disabled


def test_learn_is_listed_on_tui_command_path() -> None:
    from novacode_cli.tui.app import TUI_COMMANDS

    assert TUI_COMMANDS["learn"].handler == "_passthrough_command"


@pytest.mark.asyncio
async def test_learning_review_command_opens_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.config import config as config_module
    from novacode_cli.config import nova_config
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(
        nova_config,
        "NovaConfig",
        lambda: SimpleNamespace(
            get_learning_enabled=lambda: True,
        ),
    )
    monkeypatch.setattr(
        config_module,
        "settings",
        SimpleNamespace(
            get_agent_dir=lambda _assistant: tmp_path,
        ),
    )
    fake_app = SimpleNamespace(assistant_id="nova-agent", push_screen_wait=AsyncMock())
    await NovaApp._run_learning(fake_app, "/learning review")
    opened = fake_app.push_screen_wait.await_args.args[0]
    assert isinstance(opened, LearningScreen)
    assert opened._agent_dir == tmp_path
