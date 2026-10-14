"""A compacted history must not present completed requests as fresh work."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli.agents.context_compaction import ContextCompactionMiddleware, limits_for
from novacode_cli.prompts import render_template


def test_live_summary_marks_old_requests_historical_and_tail_current() -> None:
    middleware = ContextCompactionMiddleware(context_window=4_000)
    head = [HumanMessage(content="Build A"), AIMessage(content="A is complete")]

    message = middleware._summary_message(head, "A is complete", None, limits_for(4_000))

    assert message.additional_kwargs["user_messages"] == ["Build A"]
    assert "historical" in str(message.content).lower()
    assert "Do not repeat completed" in str(message.content)
    assert "verbatim tail" in str(message.content)
    assert "outrank the summary" not in str(message.content)


def test_summarizer_requires_explicit_pending_work() -> None:
    prompt = render_template(
        "summarization.jinja", conversation="", focus_instructions="", tail_kept=True
    )

    assert "completed" in prompt.lower()
    assert "Do not infer pending work" in prompt
