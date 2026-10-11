"""Nova's context compaction middleware, run in a real agent graph.

Each graph test drives a scripted model through a long tool-calling run inside a
4,000-token window (summary due at 2,000, recent tail of 400) and checks what the
model was actually sent on every call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

import novacode_cli.compaction as C
from novacode_cli.agents.context_compaction import (
    ContextCompactionMiddleware,
    effective_view,
    limits_for,
    mechanical_digest,
    tail_start,
    trim_old_arguments,
    user_messages,
)

WINDOW = 4_000
TASK = "TASK: build the frobnicator. Never touch /etc. The output goes in /app/out.json."
CFG = {"configurable": {"thread_id": "t"}, "recursion_limit": 300}


@tool
def work(step: int, payload: str) -> str:  # noqa: ARG001
    """Do one step of work."""
    return f"result of step {step}: " + "r" * 800


class Worker(BaseChatModel):
    """Calls ``work`` for ``steps`` steps, then answers. Also writes summaries."""

    steps: int = 30
    fail_summaries: bool = False
    overflow_above: int = 0
    refusals: int = 0
    seen: list = Field(default_factory=list)  # every work request, as sent
    summaries: list = Field(default_factory=list)  # every summary prompt
    summary_tags: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "worker"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Worker:  # noqa: ARG002
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:  # noqa: ARG002
        metadata = (run_manager.metadata if run_manager else None) or {}
        if metadata.get("lc_source") == "summarization":
            self.summaries.append(str(messages[0].content))
            self.summary_tags.append(list(run_manager.tags or []))
            if self.fail_summaries:
                raise RuntimeError("summary provider is down")
            reply = AIMessage(content=f"## Conversation Summary\nSUMMARY-{len(self.summaries)}")
            return ChatResult(generations=[ChatGeneration(message=reply)])
        if self.overflow_above and count_tokens_approximately(messages) > self.overflow_above:
            self.refusals += 1
            raise ValueError("Error code: 400 - context_length_exceeded: request too long")
        self.seen.append(list(messages))
        step = len(self.seen)
        if step > self.steps:
            reply = AIMessage(content="all done")
        else:
            call = {"name": "work", "args": {"step": step, "payload": "p" * 400}, "id": f"c{step}", "type": "tool_call"}
            reply = AIMessage(content="", tool_calls=[call])
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _agent(model: Worker, middleware: ContextCompactionMiddleware, **kwargs: Any):  # noqa: ANN202
    return create_agent(model, tools=[work], middleware=[middleware], checkpointer=InMemorySaver(), **kwargs)


def _well_formed(messages: list) -> None:
    """Every tool result has its call earlier in the same request."""
    issued: set = set()
    for message in messages:
        if isinstance(message, AIMessage):
            issued |= {call["id"] for call in message.tool_calls}
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in issued, f"orphan result {message.tool_call_id}"


def _text(message: Any) -> str:
    return str(message.content)


# ── the whole thing, in a graph ────────────────────────────────────────────


def test_a_long_run_stays_inside_the_window_and_keeps_the_task() -> None:
    model = Worker()
    agent = _agent(model, ContextCompactionMiddleware(model, context_window=WINDOW))

    out = agent.invoke({"messages": [("user", TASK)]}, CFG)

    assert out["messages"][-1].content == "all done"
    assert len(model.seen) == 31
    # Without compaction the last request would be ~9,000 tokens.
    limits = limits_for(WINDOW)
    assert max(count_tokens_approximately(sent) for sent in model.seen) < limits.trigger + 700
    assert len(model.summaries) >= 3, "one summary would not have been enough"

    for sent in model.seen:
        _well_formed(sent)
    # Once compacted, every request opens with the summary, and the summary
    # carries the task in the user's own words, however many times it is redone.
    compacted = [sent for sent in model.seen if "SUMMARY-" in _text(sent[0])]
    assert compacted and compacted[0] is not model.seen[0]
    assert all(TASK in _text(sent[0]) for sent in compacted)
    assert f"SUMMARY-{len(model.summaries)}" in _text(model.seen[-1][0])
    # The latest step is always there verbatim.
    assert all(isinstance(sent[-1], (ToolMessage, HumanMessage)) for sent in model.seen)

    # A later summary is written from the earlier one, in full.
    assert "EARLIER SUMMARY" in model.summaries[1] and "SUMMARY-1" in model.summaries[1]
    # The summary call is hidden from the token stream.
    assert all("nostream" in tags for tags in model.summary_tags)

    # State keeps every message; the event says where the model's view starts.
    values = agent.get_state(CFG).values
    assert len(values["messages"]) == 1 + 30 * 2 + 1
    view, cutoff = effective_view(values["messages"], values["_summarization_event"])
    assert cutoff is not None and view[1:] == values["messages"][cutoff:]
    _well_formed(view)


def test_the_async_path_does_the_same() -> None:
    model = Worker(steps=20)
    agent = _agent(model, ContextCompactionMiddleware(model, context_window=WINDOW))

    out = asyncio.run(agent.ainvoke({"messages": [("user", TASK)]}, CFG))

    assert out["messages"][-1].content == "all done"
    assert len(model.summaries) >= 2
    assert all(TASK in _text(sent[0]) for sent in model.seen)


def test_the_summary_never_streams_and_the_ui_is_told() -> None:
    model = Worker(steps=12)
    agent = _agent(model, ContextCompactionMiddleware(model, context_window=WINDOW))

    streamed, custom = [], []
    for mode, data in agent.stream({"messages": [("user", TASK)]}, CFG, stream_mode=["messages", "custom"]):
        if mode == "custom":
            custom.append(data)
        else:
            streamed.append(str(data[0].content))

    assert model.summaries, "the run was meant to compact"
    assert not any("SUMMARY-" in text for text in streamed)
    assert {"type": "nova_compaction_start"} in custom


def test_the_replaced_messages_are_saved_and_the_summary_says_where(tmp_path) -> None:
    from deepagents.backends import FilesystemBackend

    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    model = Worker(steps=12)
    agent = _agent(model, ContextCompactionMiddleware(model, backend=backend, context_window=WINDOW))

    agent.invoke({"messages": [("user", TASK)]}, CFG)

    values = agent.get_state(CFG).values
    path = values["_summarization_event"]["file_path"]
    assert path and path.startswith("/conversation_history/")
    assert path in _text(values["_summarization_event"]["summary_message"])
    files = sorted((tmp_path / "conversation_history").glob("*.md"))
    saved = "".join(f.read_text(encoding="utf-8") for f in files)
    assert "result of step 1:" in saved and "-> work(" in saved
    # Every file, not just the latest, is named in the summary the model has.
    assert len(files) >= 2
    assert all(f.name in _text(values["_summarization_event"]["summary_message"]) for f in files)


# ── when the summary cannot be had ─────────────────────────────────────────


def test_a_failing_summary_model_never_fails_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "SUMMARY_ATTEMPTS", 1)  # no retry sleeps
    model = Worker(fail_summaries=True)
    agent = _agent(model, ContextCompactionMiddleware(model, context_window=WINDOW))

    out = agent.invoke({"messages": [("user", TASK)]}, CFG)

    assert out["messages"][-1].content == "all done"
    # It backs off between attempts rather than asking on every call...
    assert 3 <= len(model.summaries) < 15
    # ...and after three failures compacts without a model.
    digests = [sent for sent in model.seen if "mechanical record" in _text(sent[0])]
    assert digests
    assert TASK in _text(digests[0][0])
    assert "-> work(" in _text(digests[0][0])
    for sent in model.seen:
        _well_formed(sent)


def test_a_too_long_rejection_from_the_provider_is_recovered() -> None:
    # Nova believes the window is huge; the provider disagrees at 3,000 tokens.
    model = Worker(steps=20, overflow_above=3_000)
    agent = _agent(model, ContextCompactionMiddleware(model, context_window=1_000_000))

    out = agent.invoke({"messages": [("user", TASK)]}, CFG)

    assert out["messages"][-1].content == "all done"
    assert model.summaries
    assert all(TASK in _text(sent[0]) for sent in model.seen)
    # Refused once, it works within what the provider accepts from then on.
    assert model.refusals == 1


def test_nothing_is_summarized_when_there_is_nothing_worth_removing() -> None:
    # The system prompt alone is over the trigger: summarizing three small
    # steps would cost a model call per step and free nothing.
    model = Worker(steps=3)
    agent = _agent(
        model,
        ContextCompactionMiddleware(model, context_window=WINDOW),
        system_prompt="s" * 12_000,
    )

    agent.invoke({"messages": [("user", TASK)]}, CFG)

    assert model.summaries == []


# ── stage 1: pruning tool results, and the System One opt-in ───────────────


def _history(turns: int) -> list:
    messages: list = [HumanMessage(content=TASK, id="h0")]
    for n in range(1, turns + 1):
        call = {"id": f"c{n}", "name": "read_file", "args": {"file_path": f"/src/f{n}.py"}, "type": "tool_call"}
        messages.append(AIMessage(content="", id=f"a{n}", tool_calls=[call]))
        messages.append(ToolMessage(f"contents of file {n} " + str(n) * 700, tool_call_id=f"c{n}", name="read_file", id=f"t{n}"))
    return messages


class _Request(SimpleNamespace):
    def override(self, **changes: Any) -> _Request:
        return _Request(**{**vars(self), **changes})


def _request(messages: list, model: Any = None) -> Any:
    return _Request(model=model, messages=messages, system_message=None, tools=[], state={})


def _results(messages: list) -> dict:
    return {m.tool_call_id: _text(m) for m in messages if isinstance(m, ToolMessage)}


def test_old_tool_results_are_moved_out_by_age_by_default(tmp_path) -> None:
    from novacode_cli.agents.core_agent import _tool_result_clearing

    middleware = ContextCompactionMiddleware(context_window=WINDOW, prune_edit=_tool_result_clearing(WINDOW, tmp_path))
    history = _history(10)

    sent = _results(middleware._plan(_request(history)).messages)

    assert all("/cleared/" in sent[f"c{n}"] for n in range(1, 6))
    assert all(sent[f"c{n}"].startswith("contents of file") for n in range(6, 11))
    assert len(list(tmp_path.glob("*.txt"))) == 5
    assert _results(history)["c1"].startswith("contents of file"), "state is never rewritten"


def test_with_system_one_verdicts_only_the_stale_results_go(tmp_path) -> None:
    from novacode_cli.agents.core_agent import _tool_result_clearing
    from novacode_cli.agents.tool_offload import VerdictToolUsesEdit
    from novacode_cli.agents.tool_verdicts import FakeDecisionClient, ToolVerdictCache, VerdictScorer

    stale = {"result_t1", "result_t3"}
    client = FakeDecisionClient(lambda name: 0.1 if name in stale else 0.9)
    cache = ToolVerdictCache(path=None)
    scorer = VerdictScorer(client, cache, min_interval_seconds=0.0)
    edit = _tool_result_clearing(WINDOW, tmp_path, cache, scorer)
    assert isinstance(edit, VerdictToolUsesEdit)
    middleware = ContextCompactionMiddleware(context_window=WINDOW, prune_edit=edit, scorer=scorer)
    history = _history(10)

    # The first call only queues scoring, on a background thread.
    middleware._plan(_request(history))
    scorer._thread.join(timeout=10)
    assert scorer.last_error is None and client.calls

    sent = _results(middleware._plan(_request(history)).messages)
    assert {call for call, text in sent.items() if "/cleared/" in text} == {"c1", "c3"}


def test_a_broken_scorer_never_blocks_the_call() -> None:
    class Exploding:
        def maybe_score(self, _messages: list) -> None:
            raise RuntimeError("scorer is broken")

    middleware = ContextCompactionMiddleware(context_window=WINDOW, scorer=Exploding())
    assert middleware._plan(_request(_history(2))).messages


# ── stage 2: old tool-call arguments ───────────────────────────────────────


def test_long_arguments_of_old_calls_are_trimmed_for_any_tool() -> None:
    script = "cat > /tmp/probe.py <<'EOF'\n" + "print('x')\n" * 500 + "EOF\npython /tmp/probe.py"
    old = AIMessage(content="", tool_calls=[{"id": "c1", "name": "execute", "args": {"command": script, "timeout": 30}, "type": "tool_call"}])
    recent = AIMessage(content="", tool_calls=[{"id": "c2", "name": "execute", "args": {"command": script}, "type": "tool_call"}])
    messages = [HumanMessage(content=TASK), old, ToolMessage("ok", tool_call_id="c1"), recent, ToolMessage("ok", tool_call_id="c2")]

    out = trim_old_arguments(messages, upto=3)

    trimmed = out[1].tool_calls[0]["args"]
    assert trimmed["command"].startswith("cat > /tmp/probe.py") and "trimmed to save context" in trimmed["command"]
    assert len(trimmed["command"]) < 400 and trimmed["timeout"] == 30
    assert out[3] is recent, "the recent tail is sent as it was"
    assert old.tool_calls[0]["args"]["command"] == script, "the original message is not mutated"


# ── where the tail starts ──────────────────────────────────────────────────


def test_the_tail_never_starts_on_a_tool_result_and_keeps_parallel_calls_together() -> None:
    calls = [{"id": f"p{i}", "name": "read_file", "args": {}, "type": "tool_call"} for i in range(3)]
    messages = [
        *_history(4),
        AIMessage(content="", tool_calls=calls),
        *[ToolMessage("z" * 400, tool_call_id=f"p{i}") for i in range(3)],
    ]

    for keep in (1, 50, 150, 250, 400, 10_000):
        start = tail_start(messages, keep)
        assert 0 < start < len(messages)
        assert not isinstance(messages[start], ToolMessage)
        _well_formed(messages[start:])
    assert tail_start(messages, 1) == len(messages) - 4, "the last exchange is kept even over budget"


# ── what the summary carries ───────────────────────────────────────────────


def test_user_messages_keep_the_task_and_the_most_recent_within_budget() -> None:
    head = [HumanMessage(content=TASK)] + [HumanMessage(content=f"follow-up {n} " + "w" * 300) for n in range(10)]

    said = user_messages(head, budget_chars=1_200)

    assert said[0] == TASK
    assert said[-1].startswith("follow-up 9") and len(said) < 11

    # A later compaction carries them forward from the earlier summary message.
    summary = HumanMessage(content="…", additional_kwargs={"lc_source": "summarization", "user_messages": said})
    again = user_messages([summary, HumanMessage(content="one more thing")], budget_chars=5_000)
    assert again[0] == TASK and again[-1] == "one more thing"


def test_the_mechanical_digest_lists_calls_and_how_they_ended() -> None:
    history = _history(3)
    history.append(AIMessage(content="", tool_calls=[{"id": "e1", "name": "execute", "args": {"command": "make"}, "type": "tool_call"}]))
    history.append(ToolMessage("make: *** No rule to make target", tool_call_id="e1", status="error"))

    digest = mechanical_digest(history)

    assert "/src/f2.py" in digest
    assert "→ error: make: *** No rule" in digest


# ── sizing ─────────────────────────────────────────────────────────────────


def test_the_estimate_is_scaled_by_what_the_provider_reported() -> None:
    middleware = ContextCompactionMiddleware(context_window=WINDOW)
    history = _history(5)  # ~1,100 estimated tokens: under the 2,000 trigger
    assert middleware._plan(_request(history)).cutoff == 0

    reply = AIMessage(content="", usage_metadata={"input_tokens": 2_200, "output_tokens": 5, "total_tokens": 2_205})
    for _ in range(4):
        middleware._calibrate(reply, 1_100)

    assert middleware._plan(_request(history)).cutoff > 0, "the provider counted twice as many"


def test_a_subagent_instance_takes_its_window_from_the_request_model() -> None:
    small = SimpleNamespace(profile={"max_input_tokens": 4_000})
    large = SimpleNamespace(profile={"max_input_tokens": 400_000})
    middleware = ContextCompactionMiddleware()

    assert middleware.limits(small).trigger == limits_for(4_000).trigger
    assert middleware._plan(_request(_history(12), small)).cutoff > 0
    assert middleware._plan(_request(_history(12), large)).cutoff == 0


def test_it_takes_the_place_of_the_library_summarizer() -> None:
    from deepagents.backends import StateBackend
    from deepagents.graph import _apply_custom_middleware
    from deepagents.middleware.summarization import create_summarization_middleware
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    model = FakeListChatModel(responses=["summary"])
    ours = ContextCompactionMiddleware(model, context_window=128_000)

    merged = _apply_custom_middleware([create_summarization_middleware(model, StateBackend())], [ours])

    assert merged == [ours]


def test_every_subagent_gets_it_too() -> None:
    from novacode_cli.agents.core_agent import _harden_subagent_specs

    spec = {"name": "researcher", "description": "d", "system_prompt": "p", "tools": []}
    hardened = _harden_subagent_specs([spec])

    assert any(isinstance(m, ContextCompactionMiddleware) for m in hardened[0]["middleware"])
    assert "middleware" not in spec, "the cached spec is not mutated"


def test_a_system_message_is_counted() -> None:
    middleware = ContextCompactionMiddleware(context_window=WINDOW)
    request = _request(_history(2))
    bare = middleware._plan(request).raw_tokens
    request.system_message = SystemMessage(content="s" * 4_000)
    assert middleware._plan(request).raw_tokens >= bare + 900


def test_a_huge_window_is_budgeted_unless_the_user_says_otherwise() -> None:
    from novacode_cli.context._analysis import DEFAULT_CONTEXT_BUDGET_TOKENS
    from novacode_cli.context._analysis import working_window as _working_window

    assert _working_window(1_000_000, None) == DEFAULT_CONTEXT_BUDGET_TOKENS
    assert limits_for(_working_window(1_000_000, None)).trigger < 110_000
    assert _working_window(40_960, None) == 40_960, "a small window is not raised to the budget"
    assert _working_window(1_000_000, 0) == 1_000_000, "0 means the whole window"
    assert _working_window(1_000_000, 60_000) == 60_000
    assert _working_window(0, None) == DEFAULT_CONTEXT_BUDGET_TOKENS, "unknown window"
    assert _working_window(200_000, "junk") == DEFAULT_CONTEXT_BUDGET_TOKENS


def test_the_context_bar_measures_against_the_same_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.context import ContextManager
    from novacode_cli.context import _analysis as A

    monkeypatch.setattr(A, "model_window_size", lambda _name, _dynamic=True: 1_000_000)
    setting: dict = {}
    monkeypatch.setattr(NovaConfig, "get", lambda _self, key, default=None: setting.get(key, default))

    # The number the indicator, auto-compact and the middleware all read.
    assert ContextManager("big-model").window_size() == A.DEFAULT_CONTEXT_BUDGET_TOKENS
    assert ContextManager("big-model").breakdown([]).context_window_size == A.DEFAULT_CONTEXT_BUDGET_TOKENS
    setting["context_budget_tokens"] = 0
    A._budget_read = None  # the setting is re-read every few seconds, not per call
    assert ContextManager("big-model").window_size() == 1_000_000


# ── found in review ────────────────────────────────────────────────────────


def test_injected_notes_are_not_quoted_as_the_users_words() -> None:
    head = [
        HumanMessage(TASK),
        HumanMessage("Internal context: skills that may apply. If one fits, read it."),
        HumanMessage("also keep the tests green"),
    ]
    assert user_messages(head, 10_000) == [TASK, "also keep the tests green"]


def test_a_refusal_that_shrinking_does_not_cure_is_not_remembered() -> None:
    """A misread error on a small request must not shrink the window for good."""
    middleware = ContextCompactionMiddleware(context_window=128_000)
    request = _request(_history(4))

    def refuse(_request: Any) -> Any:
        raise ValueError("context_length_exceeded")

    with pytest.raises(ValueError, match="context_length"):
        middleware.wrap_model_call(request, refuse)

    assert middleware.limits(request.model).window == 128_000


def test_a_summary_on_schedule_that_is_still_too_long_is_clipped_and_retried() -> None:
    middleware = ContextCompactionMiddleware(Worker(), context_window=WINDOW)
    history = _history(12)
    history[-1] = ToolMessage("x" * 40_000, tool_call_id=history[-1].tool_call_id)
    sizes: list[int] = []

    def handler(request: Any) -> Any:
        sizes.append(count_tokens_approximately(request.messages))
        if sizes[-1] > 5_000:
            raise ValueError("context_length_exceeded")
        return AIMessage(content="ok")

    middleware.wrap_model_call(_request(history), handler)

    assert len(sizes) == 2 and sizes[1] < 5_000 < sizes[0]


def test_pruning_does_not_wait_for_a_trigger_sized_for_a_larger_window() -> None:
    from langchain.agents.middleware import ClearToolUsesEdit

    edit = ClearToolUsesEdit(trigger=10**9, keep=1)
    middleware = ContextCompactionMiddleware(prune_edit=edit)
    small = SimpleNamespace(profile={"max_input_tokens": 4_000})

    plan = middleware._plan(_request(_history(12), small))

    assert any(m.content == edit.placeholder for m in plan.messages if isinstance(m, ToolMessage))


def test_a_model_profile_window_is_budgeted_like_the_main_agents(monkeypatch) -> None:
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr(NovaConfig, "get", lambda self, key, default=None: 50_000)
    huge = SimpleNamespace(profile={"max_input_tokens": 1_000_000})

    assert ContextCompactionMiddleware().limits(huge).window == 50_000


def test_old_results_are_checked_on_disk_once_not_on_every_model_call(tmp_path, monkeypatch) -> None:
    """Each call paid a mkdir and two stats per cleared result: 190 ms of 230."""
    from pathlib import Path

    from novacode_cli.agents.core_agent import _tool_result_clearing

    middleware = ContextCompactionMiddleware(context_window=WINDOW, prune_edit=_tool_result_clearing(WINDOW, tmp_path))
    request = _request(_history(10))
    first = middleware._plan(request).messages

    def touched(self: Path, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"filesystem touched again for {self}")

    monkeypatch.setattr(Path, "mkdir", touched)
    monkeypatch.setattr(Path, "exists", touched)

    assert _results(middleware._plan(request).messages) == _results(first)
