"""`/verbose` and `/stop` over the socket: a session toggle and a client action."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui.port_server import ServerPort


class _SS:
    """Session state with the one behaviour each command needs."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.verbose = verbose
        self.todos: list[Any] = []
        self.steering_instructions: list[Any] = []
        self.headless_out_fd = None

    def toggle_verbose(self) -> bool:
        """Mirror `SessionState.toggle_verbose`."""
        self.verbose = not self.verbose
        return self.verbose


def _port(state: _SS | None = None) -> ServerPort:
    """A port over a worker whose session state carries the behaviour."""
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


async def test_verbose_flips_and_reports_with_the_textual_wording() -> None:
    state = _SS()
    port = _port(state)
    on = await port.run("/verbose", None)
    assert on.ok is True
    assert state.verbose is True
    assert on.blocks[0].text == "Verbose mode on — internal context shown."
    off = await port.run("verbose", None)
    assert state.verbose is False
    assert off.blocks[0].text == "Verbose mode off — internal context collapsed."


async def test_verbose_without_a_session_says_so() -> None:
    worker = SimpleNamespace(
        agent=object(),
        session_state=None,
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )
    port = ServerPort(list.append.__get__([]), worker)
    result = await port.run("/verbose", None)
    assert result.ok is False
    assert result.message == "Session is unavailable."


async def test_stop_answers_with_a_cancel_action() -> None:
    port = _port()
    result = await port.run("/stop", None)
    assert result.ok is True
    assert result.action == "cancel"
    assert "Stopping" in result.message


@pytest.mark.parametrize("name", ["auto-approve", "stop", "verbose"])
def test_each_port_command_is_served_with_its_own_evidence(name: str) -> None:
    from novacode_cli.server.catalog import get

    spec = get(name)
    assert spec is not None
    assert (spec.route, spec.served, spec.evidence) == ("passthrough", True, "port-command")
