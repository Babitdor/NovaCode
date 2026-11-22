"""`/auto-approve` over the socket: the session toggle, never a stored default."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui.port_server import ServerPort


class _SS:
    """Session state with the one field the command reads."""

    def __init__(self, *, auto_approve: bool = False) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = auto_approve
        self.todos: list[Any] = []
        self.steering_instructions: list[Any] = []
        self.headless_out_fd = None


def _port(state: _SS | None = None) -> ServerPort:
    """A port over a worker whose session state carries the flag."""
    worker = SimpleNamespace(
        agent=object(),
        session_state=state or _SS(),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )
    sent: list[dict[str, Any]] = []
    return ServerPort(sent.append, worker)


async def test_a_bare_command_toggles_the_session() -> None:
    state = _SS(auto_approve=False)
    port = _port(state)
    result = await port.run("/auto-approve", None)
    assert result.ok is True
    assert state.auto_approve is True
    assert result.blocks[0].text == "Auto-approve on for this session."
    again = await port.run("auto-approve", None)
    assert state.auto_approve is False
    assert again.blocks[0].text == "Auto-approve off for this session."


@pytest.mark.parametrize(("arg", "expected"), [("on", True), ("off", False), ("ON", True)])
async def test_explicit_arguments_set_the_state(arg: str, *, expected: bool) -> None:
    state = _SS(auto_approve=not expected)
    port = _port(state)
    result = await port.run("/auto-approve", arg)
    assert result.ok is True
    assert state.auto_approve is expected


async def test_status_reports_without_changing_anything() -> None:
    state = _SS(auto_approve=True)
    port = _port(state)
    result = await port.run("/auto-approve", "status")
    assert result.ok is True
    assert state.auto_approve is True
    assert result.blocks[0].text == "Auto-approve is on for this session."


async def test_an_unknown_argument_gets_the_usage_line() -> None:
    state = _SS()
    port = _port(state)
    result = await port.run("/auto-approve", "sometimes")
    assert result.ok is False
    assert result.message == "Usage: /auto-approve [on|off|status]"
    assert state.auto_approve is False, "a refused argument must not flip the flag"


async def test_the_choice_is_never_written_to_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Textual UI promises a session preference; so does the port."""
    from novacode_cli.ui import port_server

    def explode() -> None:
        msg = "auto-approve must not touch persisted configuration"
        raise AssertionError(msg)

    monkeypatch.setattr(port_server, "_config", explode)
    state = _SS()
    port = _port(state)
    await port.run("/auto-approve", "on")
    assert state.auto_approve is True
