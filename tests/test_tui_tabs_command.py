"""`/tabs` as a picker over the server's session registry.

The list must be the same data the `sessions` frame carries, choosing a row must
name a session for the client's existing `attach` frame, and the mutating verbs
must refuse instead of pretending to spawn a session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from novacode_cli.ui.port_server import ServerPort


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        agent=object(),
        session_state=SimpleNamespace(
            thread_id="t1",
            session_id="alpha",
            auto_approve=False,
            todos=[],
            headless_out_fd=None,
        ),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )


ROWS = [
    {
        "id": "alpha",
        "name": "main",
        "cwd": "C:/work",
        "status": "running",
        "model": "test-model",
    },
    {
        "id": "beta",
        "name": "docs",
        "cwd": "C:/docs",
        "status": "idle",
        "model": "test-model",
    },
]


def _port(answer: object = None) -> tuple[ServerPort, list[Any]]:
    asked: list[Any] = []

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return answer

    return (
        ServerPort(
            lambda _frame: None,
            _worker(),
            ask_screen=ask_screen,
            session_rows=lambda: list(ROWS),
        ),
        asked,
    )


async def test_tabs_lists_every_session_and_marks_the_active_one() -> None:
    port, asked = _port(answer=None)
    result = await port.run("/tabs", None)
    assert result.ok is True
    items = asked[0].payload["items"]
    assert [item["id"] for item in items] == ["alpha", "beta"]
    assert "→" in items[0]["label"]
    assert "→" not in items[1]["label"]
    assert "running" in items[0]["detail"]


async def test_choosing_a_tab_names_the_attach_target() -> None:
    port, _ = _port(answer={"id": "beta"})
    result = await port.run("/tabs", "list")
    assert result.ok is True
    assert result.attach == "beta"
    assert "docs" in result.blocks[0].text


async def test_cancelling_leaves_the_session_alone() -> None:
    port, _ = _port(answer=None)
    result = await port.run("/tabs", None)
    assert result.attach is None
    assert "unchanged" in result.message.lower()


async def test_new_and_close_name_the_connection_action() -> None:
    port, asked = _port()
    created = await port.run("/tabs", "new docs")
    assert created.ok is True
    assert created.create == "docs"
    closed = await port.run("/tabs", "close child-1")
    assert closed.ok is True
    assert closed.close == "child-1"
    default_close = await port.run("/tabs", "close")
    assert default_close.close == ""
    assert asked == []


async def test_worktree_verbs_refuse_explicitly() -> None:
    port, asked = _port()
    for verb in ("launch", "resume"):
        result = await port.run("/tabs", f"{verb} thing")
        assert result.ok is False, verb
        assert "not ported" in result.message.lower()
    assert asked == []
