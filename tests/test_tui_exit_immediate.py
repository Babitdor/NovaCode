"""``/exit`` must quit immediately, even mid-turn.

Typing ``/exit`` while the agent was working used to be *queued* behind the
running turn (so it could take minutes). It is a control command, not a prompt,
so it now bypasses the queue, cancels the turn, and exits.
"""

from __future__ import annotations

import asyncio

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")


class _SS:
    thread_id = "t1"
    session_id = "s-abcdef12"
    auto_approve = True
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


class _FakeAgent:
    async def aget_state(self, config):
        class _V:
            values: dict = {"messages": []}

        return _V()

    async def astream(self, inp, **kw):
        return
        yield  # pragma: no cover

    async def aupdate_state(self, **kw):
        pass


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
    )


def test_exit_commands_constant_covers_the_aliases():
    from novacode_cli.tui.app import _EXIT_COMMANDS

    assert "/exit" in _EXIT_COMMANDS
    assert "/quit" in _EXIT_COMMANDS
    assert "exit" in _EXIT_COMMANDS
    assert "q" in _EXIT_COMMANDS


async def _drive_exit_mid_turn():
    from novacode_cli.tui.widgets import PromptInput

    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        exited = {"called": False, "queued": []}
        app.exit = lambda *a, **k: exited.__setitem__("called", True)
        # Simulate a turn in flight, with cancellation recorded.
        app._turn_active = True
        cancelled = {"n": 0}
        app.action_cancel_turn = lambda: cancelled.__setitem__("n", cancelled["n"] + 1)
        app._deferred_commands = exited["queued"]

        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/exit"
        prompt.post_message(PromptInput.Submitted(prompt, "/exit"))
        for _ in range(6):
            await pilot.pause()

        return exited, cancelled


def test_exit_mid_turn_quits_instead_of_queueing():
    exited, cancelled = asyncio.run(_drive_exit_mid_turn())
    assert exited["called"] is True, "/exit did not exit"
    assert exited["queued"] == [], "/exit was queued behind the turn"
    assert cancelled["n"] >= 1, "the running turn was not cancelled"


async def _drive_non_exit_command_still_queues():
    from novacode_cli.tui.widgets import PromptInput

    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.exit = lambda *a, **k: None
        app._turn_active = True
        app.action_cancel_turn = lambda: None
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "/tokens"
        prompt.post_message(PromptInput.Submitted(prompt, "/tokens"))
        for _ in range(6):
            await pilot.pause()
        return list(app._deferred_commands)


def test_other_commands_still_queue_behind_a_turn():
    """Sensitivity: the bypass must be exit-only."""
    assert asyncio.run(_drive_non_exit_command_still_queues()) == ["/tokens"]
