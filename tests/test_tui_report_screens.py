"""`/usage`, `/update`, `/subagents` and `/ui` over the socket.

`/usage` and `/update` are read-only reports: the same data the Textual modals
read, in the same order. `/subagents` is the manager's list-and-verb form, with
create and delete writing the same one-file agents. `/ui` says out loud that
layout editing belongs to the Textual frontend instead of pretending to patch a
layout the ratatui client does not draw from.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from rich.text import Text

from novacode_cli.ui.port_server import ServerPort

if TYPE_CHECKING:
    from pathlib import Path


def _state() -> SimpleNamespace:
    """Session state carrying only what these commands read."""
    return SimpleNamespace(
        thread_id="t1",
        session_id="s1",
        auto_approve=False,
        todos=[],
        steering_instructions=[],
        headless_out_fd=None,
    )


def _port(*, answer: object = None) -> tuple[ServerPort, list[Any]]:
    asked: list[Any] = []

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return answer

    worker = SimpleNamespace(
        agent=object(),
        session_state=_state(),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )
    return ServerPort(list.append.__get__([]), worker, ask_screen=ask_screen), asked


def _empty_report(_trees: list[dict[str, Any]]) -> str:
    """A session with no recorded usage."""
    return ""


class _Agents:
    """A settings stand-in rooted at one temporary agents directory."""

    project_root = None

    def __init__(self, root: Path) -> None:
        self.root = root

    def get_all_agents(self) -> list[tuple[str, Path, str]]:
        found = [(path.stem, path, "global") for path in sorted(self.root.glob("*.md"))]
        found += [
            (path.name, path, "global")
            for path in sorted(self.root.iterdir())
            if path.is_dir() and (path / "agent.md").exists()
        ]
        return found

    def get_agents_root_dir(self) -> Path:
        return self.root

    def ensure_project_agents_dir(self) -> Path:
        return self.root


@pytest.fixture(autouse=True)
def agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the port's `settings` lookup at a temporary agents directory."""
    from novacode_cli.config import config

    monkeypatch.setattr(config, "settings", _Agents(tmp_path))
    return tmp_path


async def test_usage_joins_quotas_the_session_report_and_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.commands import planner_usage, provider_usage
    from novacode_cli.tracking import usage_tree

    monkeypatch.setattr(provider_usage, "all_usage", lambda: [object()])
    monkeypatch.setattr(planner_usage, "usage_text", lambda _usage: Text("Claude Code 59%"))
    monkeypatch.setattr(usage_tree, "format_usage_report", lambda _trees: "session report")

    async def trees(_thread: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(usage_tree, "load_usage_trees", trees)
    result = await _port()[0].run("/usage", None)
    assert result.ok is True
    assert result.screen is not None
    assert result.screen.kind == "detail"
    markdown = result.screen.payload["markdown"]
    assert "Claude Code 59%" in markdown
    assert "session report" in markdown
    assert planner_usage.USAGE_NOTE in markdown


async def test_usage_says_so_when_no_provider_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.commands import provider_usage
    from novacode_cli.tracking import usage_tree
    from novacode_cli.ui.screen_specs import NO_USAGE

    monkeypatch.setattr(provider_usage, "all_usage", list)

    async def trees(_thread: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(usage_tree, "load_usage_trees", trees)
    monkeypatch.setattr(usage_tree, "format_usage_report", _empty_report)
    result = await _port()[0].run("/usage", None)
    assert result.ok is True
    assert NO_USAGE in result.screen.payload["markdown"]


async def test_usage_reports_a_failed_read_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.commands import provider_usage
    from novacode_cli.tracking import usage_tree

    def broken() -> list[Any]:
        reason = "keychain locked"
        raise OSError(reason)

    async def trees(_thread: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(provider_usage, "all_usage", broken)
    monkeypatch.setattr(usage_tree, "load_usage_trees", trees)
    result = await _port()[0].run("/usage", None)
    assert result.ok is False
    assert "keychain locked" in result.message


async def test_update_reports_the_versions_and_how_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli import updates

    monkeypatch.setattr(
        updates,
        "check_for_update",
        lambda **_kwargs: SimpleNamespace(
            current="1.2.3", latest="2.0.0", available=True, error="", release_title="Two point oh"
        ),
    )
    monkeypatch.setattr(updates, "release_details", lambda status: status)
    result = await _port()[0].run("/update", None)
    assert result.ok is True
    markdown = result.screen.payload["markdown"]
    assert "Update available · Two point oh" in markdown
    assert "nova update" in markdown


async def test_update_says_it_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import updates

    monkeypatch.setattr(
        updates,
        "check_for_update",
        lambda **_kwargs: SimpleNamespace(
            current="1.2.3", latest="1.2.3", available=False, error="", release_title=""
        ),
    )
    monkeypatch.setattr(updates, "release_details", lambda status: status)
    result = await _port()[0].run("/update", None)
    assert "up to date" in result.screen.payload["markdown"]


async def test_update_reports_a_failed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import updates

    def broken(**_kwargs: object) -> object:
        reason = "offline"
        raise OSError(reason)

    monkeypatch.setattr(updates, "check_for_update", broken)
    result = await _port()[0].run("/update", None)
    assert result.ok is False
    assert "offline" in result.message


async def test_subagents_list_names_each_agent(agents: Path) -> None:
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: reviews code\ntools: null\n---\nBe brief.\n",
        encoding="utf-8",
    )
    result = await _port()[0].run("/subagents", None)
    assert result.ok is True
    items = result.screen.payload["items"]
    assert items[0]["id"] == "reviewer"
    assert "reviews code" in items[0]["detail"]


async def test_subagents_empty_states_points_at_create() -> None:
    result = await _port()[0].run("/subagents", None)
    assert result.ok is True
    assert result.screen.payload["items"] == []
    assert "/subagents create" in result.blocks[0].text


async def test_subagents_show_returns_the_system_prompt(agents: Path) -> None:
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: reviews code\ntools: null\n---\nBe brief.\n",
        encoding="utf-8",
    )
    result = await _port()[0].run("/subagents", "show reviewer")
    assert result.ok is True
    assert "Be brief." in result.screen.payload["markdown"]


async def test_subagents_show_rejects_an_unknown_name() -> None:
    result = await _port()[0].run("/subagents", "show ghost")
    assert result.ok is False
    assert "No subagent named @ghost" in result.message


async def test_subagents_create_writes_the_agent_file(agents: Path) -> None:
    answer = {
        "name": "reviewer",
        "description": "reviews code",
        "kind": "sync",
        "scope": "global",
        "color": "Teal",
        "system_prompt": "Be brief.",
    }
    port, asked = _port(answer=answer)
    result = await port.run("/subagents", "create")
    assert asked[0].kind == "form"
    assert result.ok is True
    written = (agents / "reviewer.md").read_text(encoding="utf-8")
    assert "Be brief." in written
    assert "#14b8a6" in written
    assert "async" not in written


async def test_subagents_create_generates_a_prompt_when_blank(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli.commands import agents_commands

    async def generate(name: str, description: str) -> str:
        return f"You are {name}: {description}."

    monkeypatch.setattr(agents_commands, "_generate_agent_system_prompt", generate)
    notified: list[str] = []
    monkeypatch.setattr(
        "novacode_cli.ui.port_server._notify_async_agents_changed", lambda: notified.append("x")
    )
    answer = {
        "name": "auditor",
        "description": "audits deps",
        "kind": "async",
        "scope": "global",
        "color": "Sky Blue",
        "system_prompt": "",
    }
    port, _asked = _port(answer=answer)
    result = await port.run("/subagents", "create")
    assert result.ok is True
    written = (agents / "auditor.md").read_text(encoding="utf-8")
    assert "You are auditor: audits deps." in written
    assert "async: true" in written
    # An async agent changes what the agent server would launch.
    assert notified == ["x"]


async def test_subagents_create_rejects_a_bad_name() -> None:
    port, _asked = _port(answer={"name": "../evil", "description": "x"})
    result = await port.run("/subagents", "create")
    assert result.ok is False
    assert "letters, numbers, hyphens" in result.message


async def test_subagents_create_refuses_to_overwrite(agents: Path) -> None:
    (agents / "reviewer.md").write_text("---\nname: reviewer\n---\nold\n", encoding="utf-8")
    port, _asked = _port(
        answer={
            "name": "reviewer",
            "description": "reviews code",
            "kind": "sync",
            "scope": "global",
            "color": "Teal",
            "system_prompt": "Be brief.",
        }
    )
    result = await port.run("/subagents", "create")
    assert result.ok is False
    assert "already exists" in result.message


async def test_subagents_delete_removes_the_file_after_confirmation(agents: Path) -> None:
    path = agents / "reviewer.md"
    path.write_text("---\nname: reviewer\ndescription: d\n---\nBe brief.\n", encoding="utf-8")
    port, asked = _port(answer=True)
    result = await port.run("/subagents", "delete reviewer")
    assert asked[0].kind == "confirm"
    assert result.ok is True
    assert not path.exists()


async def test_subagents_delete_keeps_the_file_when_declined(agents: Path) -> None:
    path = agents / "reviewer.md"
    path.write_text("---\nname: reviewer\ndescription: d\n---\nBe brief.\n", encoding="utf-8")
    result = await _port(answer=False)[0].run("/subagents", "delete reviewer")
    assert result.ok is True
    assert result.message == "Kept."
    assert path.exists()


async def test_subagents_rejects_an_unknown_verb() -> None:
    result = await _port()[0].run("/subagents", "explode")
    assert result.ok is False
    assert result.message.startswith("Usage: /subagents")


async def test_ui_says_layout_editing_is_textual_only() -> None:
    result = await _port()[0].run("/ui", "inspect")
    assert result.ok is True
    text = result.blocks[0].text
    assert "Textual" in text
    assert "nothing here to patch" in text


@pytest.mark.parametrize(
    "name",
    ["subagents", "ui", "update", "usage"],
)
def test_each_new_row_has_its_own_catalogue_evidence(name: str) -> None:
    from novacode_cli.server.catalog import get

    spec = get(name)
    assert spec is not None
    assert spec.served is True
