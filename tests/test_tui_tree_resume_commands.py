"""`/tree` and `/resume` in the socket server's port.

`/tree` re-continues from any earlier message in the session's own history, so
that history (`tree.jsonl`) is the fixture. `/resume` cannot rewrite a live
server's session, so it asks the connection to spawn a child that starts with
`--continue <id>`; these tests pin the request, not the spawn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.ui.port_server import ServerPort

if TYPE_CHECKING:
    from pathlib import Path

    from novacode_cli.ui.port import ScreenSpec


class _Agent:
    """The parts of a compiled graph `/tree` touches."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.writes: list[dict[str, Any]] = []

    async def aget_state(self, _config: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(values={"messages": self.messages}, config={})

    async def aupdate_state(self, **kwargs: Any) -> None:
        self.writes.append(kwargs["values"])


def _history() -> list[Any]:
    return [
        HumanMessage("add a parser", id="h1"),
        AIMessage("Done: x.py parses.", id="a1"),
        HumanMessage("now add tests", id="h2"),
        AIMessage("Tests added.", id="a2"),
    ]


def _tree_manager(tmp_path: Path) -> SessionManager:
    """A real session manager whose tree already holds the conversation."""
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    tree = manager.history_tree("s1")
    tree.sync(
        [
            {"type": "HumanMessage", "id": "h1", "content": "add a parser"},
            {"type": "AIMessage", "id": "a1", "content": "Done: x.py parses."},
            {"type": "HumanMessage", "id": "h2", "content": "now add tests"},
        ]
    )
    return manager


def _port(
    *,
    answers: list[Any] | None = None,
    busy: bool = False,
    agent: Any = None,  # noqa: ANN401 — a fake graph
    manager: Any = None,  # noqa: ANN401 — a fake session manager
    session_id: str = "s1",
) -> tuple[ServerPort, list[Any]]:
    seen: list[Any] = []
    pending = list(answers or [])

    async def ask_screen(spec: ScreenSpec) -> Any:  # noqa: ANN401 — mirrors the hook
        seen.append(spec)
        return pending.pop(0) if pending else None

    worker = SimpleNamespace(
        agent=agent or _Agent(_history()),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=manager,
        image_tracker=None,
        backend=None,
        session_state=SimpleNamespace(thread_id="t1", session_id=session_id),
    )
    return ServerPort(object(), worker, ask_screen=ask_screen, busy=lambda: busy), seen


# ── /tree ────────────────────────────────────────────────────────────────
async def test_tree_refuses_while_a_turn_runs(tmp_path: Path) -> None:
    port, seen = _port(busy=True, manager=_tree_manager(tmp_path))
    result = await port.run("/tree", None)
    assert result.ok is False
    assert result.message == "Stop the running turn first (Esc), then /tree."
    assert seen == []


async def test_tree_needs_session_history() -> None:
    port, _seen = _port(manager=None)
    result = await port.run("/tree", None)
    assert result.ok is False
    assert result.message == "Session history is off for this run."


async def test_tree_with_an_empty_history_says_so(tmp_path: Path) -> None:
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    port, seen = _port(manager=manager)
    result = await port.run("/tree", None)
    assert result.ok is False
    assert result.message == "Nothing in this session's history yet."
    assert seen == []


async def test_tree_continues_from_an_earlier_user_message(tmp_path: Path) -> None:
    manager = _tree_manager(tmp_path)
    agent = _Agent(_history())
    port, seen = _port(answers=[{"id": "h2"}], agent=agent, manager=manager)
    result = await port.run("/tree", None)
    assert result.ok is True
    assert result.action == "clear"
    assert result.message == (
        "⑂ Moved to an earlier point (2 messages kept); your message is back in the prompt."
    )
    # The tree labels each node the way the Textual picker does.
    assert [row["label"] for row in seen[0].payload["items"]] == [
        "you: add a parser",
        "nova: Done: x.py parses.",
        "you: now add tests",
    ]
    kept = agent.writes[0]["messages"][1:]
    assert [message.id for message in kept] == ["h1", "a1"]
    headings = [block.text for block in result.blocks if block.kind == "heading"]
    assert headings[-1] == "your message, ready to send again"
    assert result.blocks[-1].text == "now add tests"


async def test_tree_from_an_assistant_answer_keeps_it(tmp_path: Path) -> None:
    manager = _tree_manager(tmp_path)
    agent = _Agent(_history())
    port, _seen = _port(answers=[{"id": "a1"}], agent=agent, manager=manager)
    result = await port.run("/tree", None)
    assert result.ok is True
    assert result.message == ("⑂ Moved to an earlier point (2 messages kept); continue from here.")
    assert all(
        block.kind != "heading" or block.text != "your message, ready to send again"
        for block in result.blocks
    )


async def test_tree_cancelled_changes_nothing(tmp_path: Path) -> None:
    agent = _Agent(_history())
    port, _seen = _port(answers=[], agent=agent, manager=_tree_manager(tmp_path))
    result = await port.run("/tree", None)
    assert result.ok is True
    assert result.message == "Cancelled."
    assert result.action is None
    assert agent.writes == []


async def test_tree_reports_a_failed_checkpoint_write(tmp_path: Path) -> None:
    class _Broken(_Agent):
        async def aupdate_state(self, **_kwargs: Any) -> None:
            message = "boom"
            raise RuntimeError(message)

    port, _seen = _port(
        answers=[{"id": "h2"}], agent=_Broken(_history()), manager=_tree_manager(tmp_path)
    )
    result = await port.run("/tree", None)
    assert result.ok is False
    assert result.message == "/tree failed; nothing was changed: boom"


# ── /resume ──────────────────────────────────────────────────────────────
def _saved(tmp_path: Path, session_id: str, *, project: Path | None, task: str = "") -> None:
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    manager.save_session(
        session_id,
        f"t-{session_id}",
        [HumanMessage("hello", id="m1")],
        "nova-agent",
        model_name="test-model",
        project_root=project,
        current_task=task,
    )


def _resume_port(tmp_path: Path, answers: list[Any] | None = None) -> tuple[ServerPort, list[Any]]:
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    port, seen = _port(answers=answers, manager=manager, session_id="current")
    return port, seen


async def test_resume_without_a_session_manager() -> None:
    port, _seen = _port(manager=None)
    result = await port.run("/resume", None)
    assert result.ok is False
    assert result.message == "Session resume is unavailable."


async def test_resume_lists_saved_sessions_and_asks_for_a_tab(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _saved(tmp_path, "sess-1", project=project, task="port the modals")
    _saved(tmp_path, "sess-2", project=project, task="")
    port, seen = _resume_port(tmp_path, answers=[{"id": "sess-1"}])
    result = await port.run("/resume", None)
    assert result.ok is True
    rows = {row["id"]: row for row in seen[0].payload["items"]}
    assert set(rows) == {"sess-1", "sess-2"}
    assert rows["sess-1"]["label"] == "port the modals"
    assert rows["sess-2"]["label"] == "proj conversation"
    assert result.create == "proj · sess-1"
    assert result.resume == "sess-1"
    assert result.message.startswith("⟲ Resuming sess-1 in a new tab (proj)")


async def test_resume_accepts_a_unique_id_prefix(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _saved(tmp_path, "abcd1234", project=project)
    port, seen = _resume_port(tmp_path)
    result = await port.run("/resume", "abcd")
    assert result.ok is True
    assert result.resume == "abcd1234"
    assert seen == []


async def test_resume_reports_an_unknown_session(tmp_path: Path) -> None:
    port, _seen = _resume_port(tmp_path)
    result = await port.run("/resume", "nope")
    assert result.ok is False
    assert result.message == "Could not resume that session: Saved session not found."


async def test_resume_refuses_a_session_without_a_project(tmp_path: Path) -> None:
    _saved(tmp_path, "loose", project=None)
    port, _seen = _resume_port(tmp_path)
    result = await port.run("/resume", "loose")
    assert result.ok is False
    assert result.message == (
        "Could not resume that session: This session has no saved project folder."
    )


async def test_resume_refuses_the_session_already_open(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _saved(tmp_path, "current", project=project)
    port, _seen = _resume_port(tmp_path)
    result = await port.run("/resume", "current")
    assert result.ok is False
    assert result.message == "That session is already open here."


async def test_resume_with_nothing_saved(tmp_path: Path) -> None:
    port, _seen = _resume_port(tmp_path)
    result = await port.run("/resume", None)
    assert result.ok is False
    assert result.message.startswith("No saved sessions available yet.")


@pytest.mark.parametrize("name", ["tree", "resume"])
def test_both_commands_are_served_by_the_catalogue(name: str) -> None:
    from novacode_cli.server.catalog import CATALOG

    row = CATALOG[name]
    assert row.served is True
    assert row.route == "screen"
