"""Slash/bash commands submitted while the agent is busy must be QUEUED as
commands (run after the turn), not steered / sent to the agent as text."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _FakeAgent:
    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})


async def _drive() -> None:
    from textual.widgets import Input, OptionList
    from novacode_cli.tui.widgets import PromptInput

    from novacode_cli.states.Session import SessionState
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    ss = SessionState()
    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=ss,
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
        session_manager=None,
    )

    slash_calls: list[str] = []
    bash_calls: list[str] = []

    async with app.run_test() as pilot:
        inp = app.query_one("#prompt", PromptInput)
        app.query_one("#cmdpalette", OptionList).display = False  # avoid palette interception
        app._turn_active = True  # simulate the agent being busy

        # A slash command queues as a command (not steered).
        app.on_input_submitted(Input.Submitted(inp, "/model"))
        await pilot.pause()
        assert "/model" in app._deferred_commands
        steers = [getattr(si, "instruction", "") for si in (ss.steering_instructions or [])]
        assert "/model" not in steers

        # A !bash command also queues as a command.
        app.on_input_submitted(Input.Submitted(inp, "!echo hi"))
        await pilot.pause()
        assert "!echo hi" in app._deferred_commands

        # A normal message still steers the running turn.
        app.on_input_submitted(Input.Submitted(inp, "keep going"))
        await pilot.pause()
        steers = [getattr(si, "instruction", "") for si in (ss.steering_instructions or [])]
        assert any("keep going" in s for s in steers)
        assert "keep going" not in app._deferred_commands

        # Draining runs each queued item through the COMMAND handlers, in order.
        async def _fake_slash(c):
            slash_calls.append(c)

        async def _fake_bash(c):
            bash_calls.append(c)

        app._run_slash = _fake_slash  # type: ignore[method-assign]
        app._run_bash = _fake_bash  # type: ignore[method-assign]
        app._turn_active = False
        await app._drain_deferred_commands()

        assert slash_calls == ["/model"]
        assert bash_calls == ["!echo hi"]
        assert app._deferred_commands == []


def test_slash_commands_queue_as_commands_not_steers() -> None:
    try:
        import textual  # noqa: F401
    except ImportError:
        return
    asyncio.run(_drive())
