"""Compaction: hierarchical summarization never overflows the summarizer, and
the summarizer budget tracks the model's context window."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import novacode_cli.compaction as C


class _MockModel:
    """Records the input size of each summarization call."""

    model_name = "mock-model"

    def __init__(self) -> None:
        self.call_input_lens: list[int] = []

    async def ainvoke(self, msgs):
        self.call_input_lens.append(len(msgs[0].content))
        return AIMessage(content="SUMMARY")


def test_short_conversation_is_single_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "_budget_chars", lambda _m, _cw=None: 2000)
    m = _MockModel()
    msgs = [HumanMessage(content="build X", id="h1"), AIMessage(content="done", id="a1")]
    out = asyncio.run(C.summarize_conversation(m, msgs))
    assert out == "SUMMARY"
    assert len(m.call_input_lens) == 1  # one LLM call


@pytest.mark.parametrize("failure", ["empty", "502", "timeout"])
def test_summary_retries_temporary_provider_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from unittest.mock import AsyncMock

    first = AIMessage(content="") if failure == "empty" else (
        TimeoutError() if failure == "timeout" else RuntimeError(
            "{'message': 'Provider returned an empty response', 'code': 502, "
            "'metadata': {'error_type': 'provider_unavailable'}}"
        )
    )
    model = _MockModel()
    model.ainvoke = AsyncMock(side_effect=[first, AIMessage(content="SUMMARY")])
    monkeypatch.setattr(C.asyncio, "sleep", AsyncMock())
    assert asyncio.run(C.summarize_conversation(model, [HumanMessage(content="task")])) == "SUMMARY"
    assert model.ainvoke.call_count == 2


def test_unavailable_summary_never_rewrites_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    model = _MockModel()
    model.ainvoke = AsyncMock(return_value=AIMessage(content=""))
    monkeypatch.setattr(C.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(C, "_archive_messages", lambda *_args: None)
    messages = [HumanMessage(content="keep this exactly", id="h1")]
    agent = _CompactAgent(messages)
    result = asyncio.run(C.compact_conversation(agent, model, "test"))
    assert not result.success
    assert "Conversation history was kept" in result.error
    assert model.ainvoke.call_count == 3
    assert agent.updated == []
    assert agent.messages == messages


def test_stalled_summary_calls_have_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    cancelled = []

    async def stalled(_messages: list) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    model = _MockModel()
    model.ainvoke = stalled
    monkeypatch.setattr(C, "SUMMARY_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(C.asyncio, "sleep", AsyncMock())
    with pytest.raises(C.SummaryUnavailableError, match="after 3 attempts"):
        asyncio.run(C.summarize_conversation(model, [HumanMessage(content="task")]))
    assert len(cancelled) == 3


def test_long_conversation_summarizes_hierarchically(monkeypatch: pytest.MonkeyPatch) -> None:
    """A conversation far larger than the budget must be chunked (multiple calls)
    rather than sent in one overflowing call."""
    budget = 1500
    monkeypatch.setattr(C, "_budget_chars", lambda _m, _cw=None: budget)

    # Measure the fixed template overhead so we can assert each call's CONVERSATION
    # portion stays within budget (template + chunk <= template + budget).
    tmpl_only = len(C.render_template("summarization.jinja", focus_instructions="", conversation=""))

    m = _MockModel()
    msgs = [HumanMessage(content="x" * 400, id=f"h{i}") for i in range(30)]  # ~12k chars >> budget
    out = asyncio.run(C.summarize_conversation(m, msgs))

    assert isinstance(out, str) and out
    assert len(m.call_input_lens) > 1, "long conversation should be chunked, not one call"
    # No single call's conversation payload exceeds the budget.
    assert max(m.call_input_lens) <= tmpl_only + budget + 64


def test_budget_uses_explicit_window_then_falls_back() -> None:
    """An explicit context_window (the TUI passes the tracker's) is honored; a
    smaller window yields a smaller budget; a floor keeps tiny windows usable."""
    small = C._budget_chars(_MockModel(), context_window=8192)
    large = C._budget_chars(_MockModel(), context_window=200_000)
    assert small < large
    assert small >= 8000  # floor so tiny windows still summarize something


def test_explicit_small_window_forces_hierarchical(monkeypatch: pytest.MonkeyPatch) -> None:
    """A conversation bigger than the (small) window is chunked — no monkeypatching
    of _budget_chars, so this exercises the real window→budget path."""
    m = _MockModel()
    # 8192-token window → ~18k-char budget; make the convo clearly larger.
    msgs = [HumanMessage(content="z" * 1500, id=f"h{i}") for i in range(40)]  # ~60k chars
    out = asyncio.run(C.summarize_conversation(m, msgs, context_window=8192))
    assert out and len(m.call_input_lens) > 1  # hierarchical kicked in


# ── Q&A gap-filling (Meta-Harness port) ────────────────────────────────────
class _QAModel:
    """Template-aware mock: summaries → 'SUMMARY', the questions pass → two
    questions, the answer pass → two answers. Records every call's input."""

    model_name = "mock-model"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.questions_calls = 0
        self.answer_calls = 0

    async def ainvoke(self, msgs):
        content = msgs[0].content
        self.calls.append(content)
        if "critical reviewer" in content:  # summarization_questions.jinja
            self.questions_calls += 1
            return AIMessage(content="Q1: What files were changed?\nQ2: What is pending?")
        if "context restorer" in content:  # summarization_answers.jinja
            self.answer_calls += 1
            return AIMessage(content="A1: src/main.py\nA2: tests are pending")
        return AIMessage(content="SUMMARY")


def test_qa_refinement_gapfills_long_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hierarchical (lossy) path runs the Q&A gap-filling pass: questions are
    asked about the summary and answered from the conversation."""
    monkeypatch.setattr(C, "_budget_chars", lambda _m, _cw=None: 4000)
    m = _QAModel()
    msgs = [HumanMessage(content="x" * 400, id=f"h{i}") for i in range(30)]  # ~12k chars
    out = asyncio.run(C.summarize_conversation(m, msgs))

    assert "## Clarifying Q&A" in out
    assert "A1: src/main.py" in out
    assert m.questions_calls == 1
    assert m.answer_calls >= 1  # per-chunk answers + merge


def test_qa_refinement_skipped_when_nothing_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the questions pass reports nothing missing (NONE), the summary is
    returned unchanged — no Q&A block, no answer calls."""
    monkeypatch.setattr(C, "_budget_chars", lambda _m, _cw=None: 4000)

    class _NoneModel:
        model_name = "mock-model"

        async def ainvoke(self, msgs):
            if "critical reviewer" in msgs[0].content:
                return AIMessage(content="NONE")
            return AIMessage(content="SUMMARY")

    m = _NoneModel()
    msgs = [HumanMessage(content="x" * 400, id=f"h{i}") for i in range(30)]
    out = asyncio.run(C.summarize_conversation(m, msgs))
    assert out == "SUMMARY"
    assert "Clarifying Q&A" not in out


def test_qa_refinement_best_effort_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure in the Q&A pass must never break compaction — the plain summary
    is returned."""
    monkeypatch.setattr(C, "_budget_chars", lambda _m, _cw=None: 4000)

    class _FailModel:
        model_name = "mock-model"

        async def ainvoke(self, msgs):
            if "critical reviewer" in msgs[0].content:
                raise RuntimeError("boom")
            return AIMessage(content="SUMMARY")

    m = _FailModel()
    msgs = [HumanMessage(content="x" * 400, id=f"h{i}") for i in range(30)]
    out = asyncio.run(C.summarize_conversation(m, msgs))
    assert out == "SUMMARY"
    assert "Clarifying Q&A" not in out


# ── compact_conversation: agent_dir → durable memory ────────────────────────
class _CompactAgent:
    """Minimal LangGraph agent: returns a fixed history, records the rewrite."""

    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.updated: list[dict] = []

    async def aget_state(self, config):
        # LangGraph state objects expose .values as a dict.
        return type("State", (), {"values": {"messages": self.messages}})()

    async def aupdate_state(self, *, config, values, as_node=None):
        self.updated.append(values)


def _compact_model():
    class _M:
        model_name = "mock-model"

        async def ainvoke(self, msgs):
            return AIMessage(content="SUMMARY")

    return _M()


def test_compact_persists_summary_to_memory_when_agent_dir_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """With agent_dir, the summary is recorded as a durable lesson and surfaced
    in result.learnings (the TUI shows '🧠 N learnings preserved to memory')."""
    monkeypatch.setattr(C, "_budget_chars", lambda _m, _cw=None: 2000)
    agent = _CompactAgent([HumanMessage(content="build X", id="h1"), AIMessage(content="done", id="a1")])
    result = asyncio.run(
        C.compact_conversation(agent, _compact_model(), "t1", agent_dir=tmp_path)
    )
    assert result.success
    assert result.learnings == "SUMMARY"
    memory = tmp_path / "memories" / "session-summary.md"
    assert memory.exists(), "summary should be persisted to durable memory"
    assert "SUMMARY" in memory.read_text(encoding="utf-8")


def test_compact_skips_memory_without_agent_dir(tmp_path) -> None:
    """No agent_dir → no memory write, learnings stays empty (the pre-existing
    callers — e.g. the CLI /compact — are unaffected)."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(C, "_budget_chars", lambda _m, _cw=None: 2000)
    agent = _CompactAgent([HumanMessage(content="build X", id="h1"), AIMessage(content="done", id="a1")])
    result = asyncio.run(C.compact_conversation(agent, _compact_model(), "t1"))
    assert result.success
    assert result.learnings == ""
    assert not (tmp_path / "memories").exists()
