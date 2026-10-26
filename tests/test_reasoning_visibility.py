"""Provider reasoning reaches the shared event stream without duplicate or opaque data."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from novacode_cli import ui_events as ev
from novacode_cli.core.agent_loop import iterate_agent_events
from novacode_cli.core.reasoning import reasoning_text
from tests.test_agent_stream import _collect, _SessionState, _State

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.mark.parametrize(
    "message",
    [
        AIMessageChunk(content="", additional_kwargs={"reasoning": "visible thought"}),
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [{"type": "reasoning.text", "text": "visible thought"}]
            },
        ),
        AIMessageChunk(
            content=[
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "visible thought"}],
                }
            ]
        ),
        AIMessage(content="answer", additional_kwargs={"reasoning_content": "visible thought"}),
        AIMessageChunk(
            content=[
                {"type": "thinking", "thinking": [{"type": "text", "text": "visible thought"}]}
            ]
        ),
        AIMessageChunk(
            content=[
                {
                    "type": "reasoning_content",
                    "reasoning_content": {"text": "visible thought", "signature": "opaque"},
                }
            ]
        ),
        AIMessageChunk(content=[{"type": "text", "text": "visible thought", "thought": True}]),
    ],
)
def test_provider_reasoning_is_visible_even_without_standard_blocks(message: AIMessage):
    class Agent:
        async def aget_state(self, _config: object) -> _State:
            return _State([])

        async def astream(self, _input: object, **_kwargs: object) -> AsyncIterator[object]:
            yield ((), "messages", (message, {}))

    events = _collect(Agent())
    assert [event.text for event in events if isinstance(event, ev.ReasoningDelta)] == [
        "visible thought"
    ]


def test_streamed_reasoning_is_not_repeated_by_completed_message():
    class Agent:
        async def aget_state(self, _config: object) -> _State:
            return _State([])

        async def astream(self, _input: object, **_kwargs: object) -> AsyncIterator[object]:
            yield (
                (),
                "messages",
                (
                    AIMessageChunk(
                        id="r", content="", additional_kwargs={"reasoning_content": "one "}
                    ),
                    {},
                ),
            )
            yield (
                (),
                "messages",
                (
                    AIMessageChunk(
                        id="r", content="", additional_kwargs={"reasoning_content": "two"}
                    ),
                    {},
                ),
            )
            yield (
                (),
                "messages",
                (
                    AIMessage(
                        id="r", content="done", additional_kwargs={"reasoning_content": "one two"}
                    ),
                    {},
                ),
            )

    events = _collect(Agent())
    assert [event.text for event in events if isinstance(event, ev.ReasoningDelta)] == [
        "one ",
        "two",
    ]


def test_normalized_reasoning_wins_over_duplicate_metadata_and_opaque_data_is_hidden():
    message = AIMessageChunk(
        content=[{"type": "thinking", "thinking": "visible", "signature": "private-signature"}],
        additional_kwargs={"reasoning_content": "visible"},
    )
    assert reasoning_text(message, message.content_blocks) == "visible"
    opaque = AIMessageChunk(
        content=[{"type": "redacted_thinking", "data": "encrypted"}],
        additional_kwargs={
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "secret"}]
        },
    )
    assert reasoning_text(opaque, opaque.content_blocks) == ""


def test_bedrock_reasoning_content_shape_is_visible_without_signature():
    block = {
        "type": "reasoning_content",
        "reasoning_content": {"text": "checking", "signature": "secret"},
    }
    assert reasoning_text(object(), [block]) == "checking"


def test_final_only_reasoning_is_not_lost_after_answer_stream_finished():
    class Agent:
        async def aget_state(self, _config: object) -> _State:
            return _State([])

        async def astream(self, _input: object, **_kwargs: object) -> AsyncIterator[object]:
            yield (
                (),
                "messages",
                (AIMessageChunk(id="final", content="answer", chunk_position="last"), {}),
            )
            yield (
                (),
                "messages",
                (
                    AIMessage(
                        id="final",
                        content="answer",
                        additional_kwargs={"reasoning": "final thought"},
                    ),
                    {},
                ),
            )

    events = _collect(Agent())
    assert [event.text for event in events if isinstance(event, ev.ReasoningDelta)] == [
        "final thought"
    ]


async def test_seen_history_does_not_reappear_as_new_thinking():
    class Agent:
        async def aget_state(self, _config: object) -> _State:
            return _State([])

        async def astream(self, _input: object, **_kwargs: object) -> AsyncIterator[object]:
            for identity in ("old", "new"):
                yield (
                    (),
                    "messages",
                    (
                        AIMessage(
                            id=identity, content="answer", additional_kwargs={"reasoning": identity}
                        ),
                        {},
                    ),
                )

    events = [
        event
        async for event in iterate_agent_events(
            "hi", Agent(), "nova-agent", _SessionState(), seen_message_ids={"old"}
        )
    ]
    assert [event.text for event in events if isinstance(event, ev.ReasoningDelta)] == ["new"]
