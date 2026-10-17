"""Messages typed while the agent works, and Esc to interrupt and redirect."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.bootstrap.steering import (
    QUEUED_SOURCE,
    SteeringInstruction,
    SteeringMiddleware,
    queued_message,
)


class _Request(SimpleNamespace):
    def override(self, **kw):
        return _Request(**{**vars(self), **kw})


def _request(messages):
    return _Request(messages=messages, system_prompt="base", system_message=None, model=None)


# -- queueing: a message typed while the agent works joins the conversation ----


def test_a_queued_message_is_recorded_in_the_conversation_once():
    from novacode_cli.events import nova_event_log

    nova_event_log.clear()
    queued = SteeringInstruction("steer", "use SQLite instead", live=True)
    middleware = SteeringMiddleware(instructions=[queued])

    update = middleware.before_model({"messages": []}, None)
    assert update is not None and len(update["messages"]) == 1
    message = update["messages"][0]
    assert message.additional_kwargs["lc_source"] == QUEUED_SOURCE
    assert message.additional_kwargs["queued_text"] == "use SQLite instead"
    assert "use SQLite instead" in message.content
    assert queued.consumed, "picked up: not re-run as a turn when this one ends"
    assert middleware.before_model({"messages": []}, None) is None, "recorded once"
    assert any("Picked up: use SQLite instead" in event[3] for event in nova_event_log)


def test_queued_messages_stay_out_of_the_system_prompt_and_the_request():
    queued = SteeringInstruction("steer", "use SQLite instead", live=True)
    middleware = SteeringMiddleware(instructions=[queued])
    middleware.before_model({"messages": []}, None)
    history = [HumanMessage("build it"), queued_message("use SQLite instead")]
    request = _request(history)
    assert middleware._inject(request) is request, "already in the history: nothing added"


def test_steer_rules_still_go_to_the_system_prompt():
    rule = SteeringInstruction("constraint", "never touch tests")
    middleware = SteeringMiddleware(instructions=[rule])
    assert middleware.before_model({"messages": []}, None) is None, "a rule is not a message"
    out = middleware._inject(_request([HumanMessage("go")]))
    assert "never touch tests" in out.system_message.content


def test_a_queued_message_after_tool_results_keeps_the_history_valid():
    """It lands after the tool results, where a user message is allowed."""
    from novacode_cli.agents.tool_call_repair import sanitize_history

    call = {"id": "c1", "name": "read_file", "args": {}, "type": "tool_call"}
    history = [
        HumanMessage("go"),
        AIMessage("", tool_calls=[call]),
        ToolMessage("x", tool_call_id="c1"),
        queued_message("also add docs"),
    ]
    assert sanitize_history(history) is history


# -- interrupt-and-redirect -----------------------------------------------------


async def _esc(app, pilot, text):
    from novacode_cli.tui.widgets import PromptInput

    app._turn_active = True
    box = app.query_one("#prompt", PromptInput)
    box.text = text
    app.action_cancel_turn()
    await pilot.pause()
    return box


@pytest.mark.asyncio
async def test_esc_with_a_message_interrupts_and_sends_it_next():
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._deferred_prompts = ["an unread queued message"]
        box = await _esc(app, pilot, "actually, use Postgres")
        assert box.text == "" and app._redirect_prompt == "actually, use Postgres"
        app._turn_active = False
        app._redirect_after_turn()
        assert app._deferred_prompts == ["an unread queued message\n\nactually, use Postgres"], (
            "one next turn, the latest instruction last"
        )


@pytest.mark.asyncio
async def test_esc_with_an_empty_box_or_a_command_is_a_plain_cancel():
    from tests.test_tui_compaction_row import _app

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _esc(app, pilot, "")
        assert not getattr(app, "_redirect_prompt", None)
        box = await _esc(app, pilot, "/compact")
        assert box.text == "/compact" and not getattr(app, "_redirect_prompt", None)


# -- /resume shows the queued message as the user typed it -----------------------


@pytest.mark.asyncio
async def test_replay_shows_a_queued_message_inline():
    from tests.test_tui_compaction_row import _app
    from novacode_cli.tui.widgets import ChatMessage

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._restored_messages = [
            HumanMessage("build the parser", id="h1"),
            queued_message("use SQLite instead"),
            AIMessage("Done, with SQLite.", id="a1"),
        ]
        app._replay_history()
        for _ in range(20):
            await pilot.pause(0.05)
        said = [m.raw_text for m in app._transcript().query(ChatMessage)]
        assert "use SQLite instead" in said, said
        assert not any("Sent by the user while" in s for s in said)
