"""Tests for the UI-agnostic agent event stream (Phase 0 of the Textual migration).

These exercise ``run_agent_stream`` against a mocked ``agent.astream`` and assert
the emitted :mod:`novacode_cli.ui_events` sequence — the contract both the legacy
renderer and the new Textual UI rely on.

Runnable directly (``python tests/test_agent_stream.py``) or via pytest.
"""

from __future__ import annotations

import asyncio
import types

import novacode_cli.ui_events as ev
from novacode_cli.core.agent_loop import iterate_agent_events


class _Chunk:
    """A fake AIMessageChunk. Name must NOT be 'AIMessage' (that marks a completed,
    non-streamed message whose text is intentionally not re-accumulated)."""

    def __init__(self, mid, blocks, *, usage=None):
        self.id = mid
        self._blocks = blocks
        self.usage_metadata = usage or {"input_tokens": 12, "output_tokens": 4}

    @property
    def content_blocks(self):
        return self._blocks


class _State:
    def __init__(self, msgs):
        self.values = {"messages": msgs}


class _SessionState:
    thread_id = "t1"


def _collect(agent, user_input="hi", on_interrupt=None):
    """Drive iterate_agent_events to completion, resolving interrupts via on_interrupt."""

    async def _run():
        out = []
        async for e in iterate_agent_events(user_input, agent, "nova-agent", _SessionState()):
            out.append(e)
            if isinstance(e, ev.InterruptRequest):
                resp = on_interrupt(e) if on_interrupt else None
                e.future.set_result(resp)
        return out

    return asyncio.run(_run())


def test_usage_sums_calls_but_context_uses_latest_call():
    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            for mid, usage in [
                ("first", {"input_tokens": 2000, "output_tokens": 20}),
                ("first", {"input_tokens": 2000, "output_tokens": 30}),
                ("last", {"input_tokens": 500, "output_tokens": 40,
                          "input_token_details": {"cache_read": 100}}),
            ]:
                yield ((), "messages", (_Chunk(mid, [], usage=usage), {}))

    updates = [e for e in _collect(Agent()) if isinstance(e, ev.UsageUpdate)]
    assert len(updates) == 1
    assert updates[0].input_tokens == 500
    assert updates[0].output_tokens == 40
    assert updates[0].cache_read_tokens == 100
    assert updates[0].session_tokens == 2570


def test_happy_path_text_tool_todo():
    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "Hello "}]), {}))
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "world"}]), {}))
            yield (
                (),
                "messages",
                (
                    _Chunk(
                        "m2",
                        [
                            {
                                "type": "tool_call_chunk",
                                "name": "shell",
                                "id": "tc1",
                                "args": {"command": "ls"},
                                "index": 0,
                            }
                        ],
                    ),
                    {},
                ),
            )
            yield ((), "updates", {"model": {"todos": [{"content": "x", "status": "pending"}]}})

        async def aupdate_state(self, **kw):
            pass

    evts = _collect(Agent())
    kinds = [type(e).__name__ for e in evts]

    assert any(isinstance(e, ev.AssistantMessage) and e.text == "Hello world" for e in evts), kinds
    assert any(isinstance(e, ev.ToolCall) and e.name == "shell" for e in evts), kinds
    assert any(isinstance(e, ev.TodoUpdate) for e in evts), kinds
    assert any(isinstance(e, ev.UsageUpdate) for e in evts), kinds
    assert isinstance(evts[0], ev.StatusUpdate)
    assert isinstance(evts[-1], ev.Done) and evts[-1].had_response


def test_question_interrupt_resumes():
    class Agent:
        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            self.calls += 1
            if self.calls == 1:
                # Pause with a question interrupt.
                yield (
                    (),
                    "updates",
                    {
                        "__interrupt__": [
                            types.SimpleNamespace(
                                value={"type": "question", "request": {"prompt": "pick"}},
                                id="i1",
                            )
                        ]
                    },
                )
            else:
                # Resumed: emit the final answer.
                yield (
                    (),
                    "messages",
                    (_Chunk("m9", [{"type": "text", "text": "done"}]), {}),
                )

        async def aupdate_state(self, **kw):
            pass

    seen = {}

    def on_interrupt(req: ev.InterruptRequest):
        seen["kind"] = req.kind
        seen["payload"] = req.payload
        return {"answer": "A"}  # question response object

    evts = _collect(Agent(), on_interrupt=on_interrupt)

    assert seen["kind"] == "question"
    assert seen["payload"] == {"prompt": "pick"}
    assert any(isinstance(e, ev.AssistantMessage) and e.text == "done" for e in evts), [
        type(e).__name__ for e in evts
    ]
    assert isinstance(evts[-1], ev.Done)


def test_approval_interrupt_fires_and_clears_notification():
    """An interrupt raises an 'approval' notification BEFORE it's surfaced (so the
    badge/hook fire the moment permission is needed) and clears it once resolved."""

    class _RecordingSS:
        thread_id = "t1"

        def __init__(self):
            self.added: list = []
            self.registered: list = []
            self.dismissed: list = []
            self._n = 0

        def add_notification(
            self, level, title, message, source, *, action_id=None, action_type=None
        ):
            self._n += 1
            nid = f"n{self._n}"
            self.added.append((level, action_id, action_type, nid))
            return nid

        def register_pending_approval(self, action_id, future):
            self.registered.append(action_id)

        def dismiss_notification(self, nid):
            self.dismissed.append(nid)
            return True

    class Agent:
        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            self.calls += 1
            if self.calls == 1:
                yield (
                    (),
                    "updates",
                    {
                        "__interrupt__": [
                            types.SimpleNamespace(
                                value={"type": "question", "request": {"prompt": "pick"}},
                                id="i1",
                            )
                        ]
                    },
                )
            else:
                yield ((), "messages", (_Chunk("m9", [{"type": "text", "text": "done"}]), {}))

        async def aupdate_state(self, **kw):
            pass

    ss = _RecordingSS()

    async def _run():
        async for e in iterate_agent_events("hi", Agent(), "nova-agent", ss):
            if isinstance(e, ev.InterruptRequest):
                # Raised BEFORE the interrupt surfaced (the ordering fix).
                assert ss.added, "notification must precede the interrupt"
                e.future.set_result({"answer": "A"})

    asyncio.run(_run())

    assert ss.added, ss.added
    level, action_id, action_type, nid = ss.added[0]
    assert level == "approval"
    assert action_id == "i1"
    assert action_type == "select"
    assert "i1" in ss.registered
    assert nid in ss.dismissed  # cleared once resolved


def test_approval_notification_suppressed_when_auto_approving():
    """A /remote turn auto-approves (auto_approve=True) — no permission is asked of
    the user, so the approval notification must NOT fire (it would just be noise)."""

    class _RecordingSS:
        thread_id = "t1"
        auto_approve = True  # e.g. a /remote turn

        def __init__(self):
            self.added: list = []
            self.registered: list = []

        def add_notification(self, *a, **kw):
            self.added.append((a, kw))
            return "n1"

        def register_pending_approval(self, action_id, future):
            self.registered.append(action_id)

        def dismiss_notification(self, nid):
            return True

    class Agent:
        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            self.calls += 1
            if self.calls == 1:
                yield (
                    (),
                    "updates",
                    {
                        "__interrupt__": [
                            types.SimpleNamespace(
                                value={"type": "question", "request": {"prompt": "pick"}},
                                id="i1",
                            )
                        ]
                    },
                )
            else:
                yield ((), "messages", (_Chunk("m9", [{"type": "text", "text": "done"}]), {}))

        async def aupdate_state(self, **kw):
            pass

    ss = _RecordingSS()

    async def _run():
        async for e in iterate_agent_events("hi", Agent(), "nova-agent", ss):
            if isinstance(e, ev.InterruptRequest):
                e.future.set_result({"answer": "A"})

    asyncio.run(_run())

    # The interrupt was still surfaced + resolved, but no notification was raised.
    assert ss.added == [], ss.added
    assert ss.registered == [], ss.registered


class _PolicyRecordingSS:
    """Session state that records notifications and never auto-approves."""

    thread_id = "t1"
    auto_approve = False
    plan_mode_enabled = False

    def __init__(self):
        self.added: list = []
        self.registered: list = []

    def add_notification(self, *a, **kw):
        self.added.append((a, kw))
        return "n1"

    def register_pending_approval(self, action_id, future):
        self.registered.append(action_id)

    def dismiss_notification(self, nid):
        return True


def _tool_interrupt_agent(command):
    """Agent that raises a tool HITL interrupt for ``command`` then answers."""

    class Agent:
        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            self.calls += 1
            if self.calls == 1:
                yield (
                    (),
                    "updates",
                    {
                        "__interrupt__": [
                            types.SimpleNamespace(
                                value={
                                    "action_requests": [
                                        {
                                            "name": "shell",
                                            "args": {"command": command},
                                            "description": "run",
                                        }
                                    ],
                                    "review_configs": [],
                                },
                                id="i1",
                            )
                        ]
                    },
                )
            else:
                yield ((), "messages", (_Chunk("m9", [{"type": "text", "text": "done"}]), {}))

        async def aupdate_state(self, **kw):
            pass

    return Agent()


def _drive(agent, ss, on_interrupt=None):
    seen = {"interrupts": 0}

    async def _run():
        async for e in iterate_agent_events("hi", agent, "nova-agent", ss):
            if isinstance(e, ev.InterruptRequest):
                seen["interrupts"] += 1
                e.future.set_result(
                    on_interrupt(e)
                    if on_interrupt
                    else {"decisions": [{"type": "approve"}], "any_rejected": False}
                )

    asyncio.run(_run())
    return seen


def test_policy_allow_tool_resolves_without_interrupt():
    """A policy-allowed command (``git status``) is auto-approved in the loop:
    no InterruptRequest is surfaced and no approval notification is raised."""
    from novacode_cli.security.policy import reset_policy_cache

    reset_policy_cache()
    ss = _PolicyRecordingSS()
    seen = _drive(_tool_interrupt_agent("git status"), ss)
    assert seen["interrupts"] == 0
    assert ss.added == []


def test_policy_deny_tool_rejected_without_prompt():
    """A policy-denied command (``rm -rf /``) is rejected without a prompt."""
    from novacode_cli.security.policy import reset_policy_cache

    reset_policy_cache()
    ss = _PolicyRecordingSS()
    seen = _drive(_tool_interrupt_agent("rm -rf /"), ss)
    assert seen["interrupts"] == 0
    assert ss.added == []


def test_policy_ask_tool_still_interrupts():
    """An ambiguous command still surfaces the InterruptRequest + notification."""
    from novacode_cli.security.policy import reset_policy_cache

    reset_policy_cache()
    ss = _PolicyRecordingSS()
    seen = _drive(_tool_interrupt_agent("frobnicate --now"), ss)
    assert seen["interrupts"] == 1
    assert ss.added, "ask path must raise an approval notification"


def test_enter_plan_mode_engages_read_only_enforcement():
    """When the core agent calls enter_plan_mode, the loop flips the session's
    plan_mode_enabled flag so the pre-HITL gate starts blocking writes/shell."""

    class _SS:
        thread_id = "t1"
        plan_mode_enabled = False

    ss = _SS()

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            yield (
                (),
                "messages",
                (
                    _Chunk(
                        "m1",
                        [
                            {
                                "type": "tool_call_chunk",
                                "name": "enter_plan_mode",
                                "id": "tc1",
                                "args": {"reason": "complex refactor"},
                                "index": 0,
                            }
                        ],
                    ),
                    {},
                ),
            )

        async def aupdate_state(self, **kw):
            pass

    async def _run():
        async for _e in iterate_agent_events("hi", Agent(), "nova-agent", ss):
            pass

    asyncio.run(_run())
    assert ss.plan_mode_enabled is True


def test_out_of_band_hermes_messages_are_dropped():
    """Hermes review/refine model calls run out-of-band but inherit the graph's
    message-stream callback, so their output (e.g. a regenerated SKILL.md) shows
    up in the 'messages' stream tagged nova_oob. It must never render as a Nova
    assistant message — only the real agent text does."""

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            # Real agent token.
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "real answer"}]), {}))
            # Out-of-band refine leak (explicit marker).
            yield (
                (),
                "messages",
                (
                    _Chunk("m2", [{"type": "text", "text": "--- name: leaked-skill ---"}]),
                    {"nova_oob": True},
                ),
            )
            # Out-of-band review leak (tag-based).
            yield (
                (),
                "messages",
                (
                    _Chunk("m3", [{"type": "text", "text": "LEAKED LESSON"}]),
                    {"tags": ["nova", "hermes", "oob-review"]},
                ),
            )

        async def aupdate_state(self, **kw):
            pass

    evts = _collect(Agent())
    deltas = [e.text for e in evts if isinstance(e, ev.TextDelta)]
    assert "real answer" in deltas
    assert all("leaked-skill" not in t for t in deltas), deltas
    assert all("LEAKED" not in t for t in deltas), deltas
    msgs = [e.text for e in evts if isinstance(e, ev.AssistantMessage)]
    assert all("leaked-skill" not in m and "LEAKED" not in m for m in msgs), msgs


def test_text_streams_as_deltas_then_commits():
    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "Hel"}]), {}))
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "lo"}]), {}))

        async def aupdate_state(self, **kw):
            pass

    evts = _collect(Agent())
    kinds = [type(e).__name__ for e in evts]
    # Two streamed deltas, then a committed message — deltas before the commit.
    assert kinds.count("TextDelta") == 2, kinds
    assert "AssistantMessage" in kinds, kinds
    assert kinds.index("TextDelta") < kinds.index("AssistantMessage"), kinds
    msg = next(e for e in evts if isinstance(e, ev.AssistantMessage))
    assert msg.text == "Hello"


def test_usage_captured_from_empty_final_chunk():
    """Providers attach usage_metadata to a final chunk with NO content blocks.

    Regression: the usage capture used to run after an empty-blocks early-continue,
    so those token counts were dropped and /context always showed 0.
    """

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            # Real text, but no usage on the content chunks...
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": "Hi"}], usage={}), {}))
            # ...then a final, content-less chunk that carries the usage.
            yield (
                (),
                "messages",
                (_Chunk("m1", [], usage={"input_tokens": 4321, "output_tokens": 99}), {}),
            )

        async def aupdate_state(self, **kw):
            pass

    evts = _collect(Agent())
    usage = next((e for e in evts if isinstance(e, ev.UsageUpdate)), None)
    assert usage is not None, [type(e).__name__ for e in evts]
    assert usage.input_tokens == 4321
    assert usage.output_tokens == 99


def test_cancellation_emits_cancelled():
    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            raise asyncio.CancelledError
            yield  # pragma: no cover (makes this an async generator)

        async def aupdate_state(self, **kw):
            pass

    evts = _collect(Agent())
    assert any(isinstance(e, ev.Cancelled) for e in evts), [type(e).__name__ for e in evts]


def test_plan_auto_approve_completes_turn():
    class MockInterrupt:
        def __init__(self, id, value):
            self.id = id
            self.value = value

    class Agent:
        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            self.calls += 1
            if self.calls == 1:
                yield (
                    (),
                    "updates",
                    {
                        "__interrupt__": [
                            MockInterrupt(
                                "int1",
                                {
                                    "type": "plan_approval",
                                    "plan": "Mock Plan content",
                                }
                            )
                        ]
                    }
                )
            else:
                yield (
                    (),
                    "messages",
                    (
                        _Chunk("m1", [{"type": "text", "text": "Plan approved, proceeding."}]),
                        {}
                    )
                )

        async def aupdate_state(self, **kw):
            pass

    class CustomSessionState:
        thread_id = "t1"
        auto_approve = True
        plan_mode_enabled = True
        plan_content = None
        approved_plan_content = None
        plan_agent = object()

        def set_approved_plan(self, plan):
            self.approved_plan_content = plan

        def clear_plan_agent(self):
            pass

    session = CustomSessionState()

    async def _run():
        out = []
        async for e in iterate_agent_events("test", Agent(), "nova-agent", session):
            out.append(e)
        return out

    evts = asyncio.run(_run())
    kinds = [type(e).__name__ for e in evts]

    assert any(isinstance(e, ev.ContextMessage) and "switching to execution mode" in e.message for e in evts), kinds
    assert not session.plan_mode_enabled
    assert session.approved_plan_content == "Mock Plan content"


def test_policy_denied_retry_loop_is_bounded():
    """A tool the policy auto-rejects, retried forever by the model, must not spin.

    Resuming after an interrupt resets LangGraph's recursion counter, so before
    the guard this looped indefinitely with no user in the way — the CLI just
    reprinted the same turn until it died.
    """
    from novacode_cli.core.agent_loop import MAX_AUTO_REJECT_RESUMES
    from novacode_cli.security.policy import reset_policy_cache

    reset_policy_cache()

    class StubbornAgent:
        """Always asks for the same policy-denied command; never gives up."""

        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            self.calls += 1
            assert self.calls <= MAX_AUTO_REJECT_RESUMES, "loop guard did not stop the turn"
            yield (
                (),
                "updates",
                {
                    "__interrupt__": [
                        types.SimpleNamespace(
                            value={
                                "action_requests": [
                                    {
                                        "name": "shell",
                                        "args": {"command": "rm -rf /"},
                                        "description": "run",
                                    }
                                ],
                                "review_configs": [],
                            },
                            id="i1",
                        )
                    ]
                },
            )

        async def aupdate_state(self, **kw):
            pass

    agent = StubbornAgent()
    ss = _PolicyRecordingSS()
    evts = []

    async def _run():
        async for e in iterate_agent_events("hi", agent, "nova-agent", ss):
            evts.append(e)
            if isinstance(e, ev.InterruptRequest):  # never expected on the deny path
                e.future.set_result({"decisions": [], "any_rejected": True})

    asyncio.run(_run())

    assert agent.calls == MAX_AUTO_REJECT_RESUMES, agent.calls
    assert any(isinstance(e, ev.Error) and "retried a blocked action" in e.message for e in evts), [
        type(e).__name__ for e in evts
    ]


def test_user_rejection_does_not_trip_the_guard():
    """The cap counts only rejections nobody was asked about — a human saying no
    to an ambiguous command each time is a choice, not a runaway loop."""
    from novacode_cli.core.agent_loop import MAX_AUTO_REJECT_RESUMES
    from novacode_cli.security.policy import reset_policy_cache

    reset_policy_cache()
    _ROUNDS = MAX_AUTO_REJECT_RESUMES + 2

    class AskingAgent:
        def __init__(self):
            self.calls = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            self.calls += 1
            if self.calls > _ROUNDS:  # outlived the cap → guard stayed out of the way
                yield ((), "messages", (_Chunk("m9", [{"type": "text", "text": "done"}]), {}))
                return
            yield (
                (),
                "updates",
                {
                    "__interrupt__": [
                        types.SimpleNamespace(
                            value={
                                "action_requests": [
                                    {
                                        "name": "shell",
                                        "args": {"command": "frobnicate --now"},
                                        "description": "run",
                                    }
                                ],
                                "review_configs": [],
                            },
                            id="i1",
                        )
                    ]
                },
            )

        async def aupdate_state(self, **kw):
            pass

    agent = AskingAgent()
    seen = _drive(
        agent,
        _PolicyRecordingSS(),
        on_interrupt=lambda _e: {"decisions": [{"type": "reject"}], "any_rejected": True},
    )
    assert seen["interrupts"] == _ROUNDS
    assert agent.calls == _ROUNDS + 1


# -- library auto-summarization backstop --------------------------------------


class _SummarizedMsg:
    """A message tagged the way SummarizationMiddleware tags its output."""

    id = "sum1"
    additional_kwargs = {"lc_source": "summarization"}


def _summarizing_agent(text: str):
    """Agent that summarizes mid-turn, then streams *text* as assistant prose."""

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            # SummarizationMiddleware rewrote history mid-turn.
            yield ((), "updates", {"model": {"messages": [_SummarizedMsg()]}})
            # Its summary streams back as ordinary assistant tokens.
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": text}]), {}))

        async def aupdate_state(self, **kw):
            pass

    return Agent()


def test_auto_compaction_is_announced_not_silent():
    """Hide the internal summary and start the TUI's compaction indicator."""
    evts = _collect(_summarizing_agent("## SESSION INTENT" + chr(10) * 2 + "Build the thing"))

    # The summary itself never becomes an assistant message.
    assert not any(isinstance(e, ev.AssistantMessage) for e in evts)
    # The Session Intent heading is held until recognized and never streamed.
    assert not any(isinstance(e, ev.TextDelta) for e in evts)
    # The UI is told to show its animated compaction status.
    starts = [
        e
        for e in evts
        if isinstance(e, ev.ContextMessage) and e.event_type == "nova_compaction_start"
    ]
    assert len(starts) == 1, [type(e).__name__ for e in evts]
    assert "compacting" in starts[0].message.lower()


def test_normal_prose_after_summarization_is_kept_and_unannounced():
    """Only the summary block is suppressed — a real answer still commits.

    Guards the obvious over-reach: announcing (or dropping) every message that
    happens to follow a summarization event.
    """
    evts = _collect(_summarizing_agent("Here is the actual answer."))

    assert any(
        isinstance(e, ev.AssistantMessage) and e.text == "Here is the actual answer."
        for e in evts
    )
    assert not any(
        isinstance(e, ev.ContextMessage) and e.event_type == "nova_auto_compact"
        for e in evts
    )


def _plain_agent(text: str):
    """Agent that streams *text* with NO preceding summarization update event."""

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": text}]), {}))

        async def aupdate_state(self, **kw):
            pass

    return Agent()


def test_summary_suppressed_without_a_preceding_update_event():
    """The reported bug: SESSION INTENT rendered anyway.

    _post_summarization is set only from a main-agent `updates` chunk carrying
    an lc_source="summarization" message. That chunk does not reliably arrive
    before the summary streams, and when it didn't the block fell through to
    the transcript. Detection now keys off the text itself.
    """
    evts = _collect(_plain_agent("## SESSION INTENT" + chr(10) * 2 + "Build the thing"))

    assert not any(isinstance(e, ev.AssistantMessage) for e in evts)
    assert any(
        isinstance(e, ev.ContextMessage) and e.event_type == "nova_compaction_start"
        for e in evts
    )


def test_session_intent_heading_is_not_streamed_when_split_across_chunks():
    """The preview hold must span provider chunks, not just a whole phrase."""

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            for part in ("## SESS", "ION INTENT", "\n\nKeep working"):
                yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": part}]), {}))

        async def aupdate_state(self, **kw):
            pass

    events = _collect(Agent())
    assert not any(isinstance(event, ev.TextDelta) for event in events)
    assert not any(isinstance(event, ev.AssistantMessage) for event in events)


def test_ordinary_heading_releases_all_buffered_preview_text():
    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            for part in ("## S", "etup", "\nInstall the package."):
                yield ((), "messages", (_Chunk("m1", [{"type": "text", "text": part}]), {}))

        async def aupdate_state(self, **kw):
            pass

    events = _collect(Agent())
    preview = "".join(event.text for event in events if isinstance(event, ev.TextDelta))
    assert preview == "## Setup\nInstall the package."
    assert not any(
        isinstance(event, ev.ContextMessage) and event.event_type == "nova_compaction_start"
        for event in events
    )


def test_summary_suppressed_after_a_lead_in_sentence():
    """Models precede the block with prose; a prefix-only check missed those."""
    body = "Here is the extracted context:" + chr(10) * 2 + "## SESSION INTENT" + chr(10) * 2 + "X"
    evts = _collect(_plain_agent(body))
    assert not any(isinstance(e, ev.AssistantMessage) for e in evts)


def test_real_answer_with_summary_and_next_steps_is_kept():
    """Guard the over-reach: those headings alone are ordinary answer sections.

    Keying on "two known section headings" swallowed genuine answers that had
    both '## Summary' and '## Next steps', which is worse than the bug.
    """
    body = (
        "I refactored the module." + chr(10) * 2
        + "## Summary" + chr(10) * 2 + "Pruning happens during the walk." + chr(10) * 2
        + "## Next steps" + chr(10) * 2 + "- review the diff"
    )
    evts = _collect(_plain_agent(body))
    assert any(
        isinstance(e, ev.AssistantMessage) and "refactored the module" in e.text
        for e in evts
    ), [type(e).__name__ for e in evts]


def _counting_agent(reply: str):
    """Agent that records how many turns it was driven for."""

    class Agent:
        turns = 0

        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            Agent.turns += 1
            yield ((), "messages", (_Chunk(f"m{Agent.turns}", [{"type": "text", "text": reply}]), {}))

        async def aupdate_state(self, **kw):
            pass

    return Agent()


def _drive_stream(agent, session_state):
    async def _run():
        return [
            e
            async for e in agent_stream_mod().run_agent_stream(
                "hi", agent, "nova-agent", session_state
            )
        ]

    return asyncio.run(_run())


def agent_stream_mod():
    import novacode_cli.agent_stream as m

    return m


def test_goal_mode_actually_loops_through_the_tui_entry_point():
    """A goal must drive MULTIPLE turns via run_agent_stream, not just one.

    Stubbing run_with_goal only proves an import is wired; it cannot catch the
    loop failing to continue. Drive the real autonomous loop and count turns.
    """

    class _GoalState(_SessionState):
        active_goal = "build a CSV parser"
        auto_approve = True

    agent = _counting_agent("still working")
    evts = _drive_stream(agent, _GoalState())

    assert type(agent).turns > 1, (
        f"goal mode ran only {type(agent).turns} turn(s) — the loop did not continue"
    )
    assert isinstance(evts[-1], ev.Done)


def test_goal_achieved_stops_the_loop():
    """The loop must stop early when the agent reports GOAL ACHIEVED."""

    class _GoalState(_SessionState):
        active_goal = "build a CSV parser"
        auto_approve = True

    agent = _counting_agent("All done. GOAL ACHIEVED")
    _drive_stream(agent, _GoalState())
    assert type(agent).turns == 1, (
        f"GOAL ACHIEVED should end the run, but it ran {type(agent).turns} turns"
    )


def test_no_goal_runs_exactly_one_turn():
    """Without a goal the entry point stays a single-turn passthrough."""
    agent = _counting_agent("done")
    evts = _drive_stream(agent, _SessionState())
    assert type(agent).turns == 1, f"expected 1 turn, got {type(agent).turns}"
    assert isinstance(evts[-1], ev.Done)


def test_a_held_lease_does_not_block_the_turn():
    """The session lease is best-effort: a conflict must not stall a TUI turn."""
    from novacode_cli.sessions.lease import lease_session

    class _State2(_SessionState):
        thread_id = "t-contended-regression"
        auto_approve = True

    agent = _counting_agent("done")

    async def _run():
        async with lease_session("t-contended-regression", "pid:other:tui"):
            return [
                e
                async for e in agent_stream_mod().run_agent_stream(
                    "hi", agent, "nova-agent", _State2()
                )
            ]

    evts = asyncio.run(_run())
    assert isinstance(evts[-1], ev.Done), "turn did not complete while the lease was held"


def test_quickjs_custom_events_become_panel_rows():
    """The eval fan-out's own events reach the panel, live.

    ``langchain-quickjs`` publishes one ``subagent`` event per ``task()`` dispatch
    on LangGraph's ``custom`` stream. Nothing else in the loop reads that stream,
    so without this branch a fan-out produced no panel rows at all while it ran.
    """

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            assert "custom" in kw.get("stream_mode", []), (
                "the loop stopped subscribing to the custom stream"
            )
            yield (
                (),
                "custom",
                {
                    "type": "subagent",
                    "phase": "start",
                    "id": "ptc_task_ab12cd34",
                    "eval_id": "call_eval_1",
                    "subagent_type": "researcher",
                    "label": "research: docling",
                    "description": "research: docling",
                },
            )
            yield (
                (),
                "custom",
                {
                    "type": "subagent",
                    "phase": "complete",
                    "id": "ptc_task_ab12cd34",
                    "eval_id": "call_eval_1",
                    "duration_ms": 33600,
                },
            )
            # Middleware writing anything else to the custom stream must cost
            # nothing: a foreign type, and a payload that is not even a mapping.
            yield ((), "custom", {"type": "some_other_middleware"})
            yield ((), "custom", "not a dict")

        async def aupdate_state(self, **kw):
            pass

    rows = [e for e in _collect(Agent()) if isinstance(e, ev.SubagentTask)]

    assert [r.status for r in rows] == ["running", "done"], rows
    assert rows[0].task_id == "ptc_task_ab12cd34"
    # eval_id is the phase, which is what groups one fan-out.
    assert rows[0].phase_id == "call_eval_1"
    assert rows[0].phase_kind == "eval"
    assert rows[0].label == "research: docling"
    # The completing event carries the duration the bridge measured.
    assert rows[1].duration_ms == 33600


def test_sync_task_dispatch_becomes_a_panel_row_while_it_runs():
    """A blocking ``task`` call is on the panel from the moment it is dispatched."""

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            yield (
                (),
                "messages",
                (
                    _Chunk(
                        "m1",
                        [
                            {
                                "type": "tool_call_chunk",
                                "name": "task",
                                "id": "tc_task_1",
                                "args": {
                                    "description": "review auth.py",
                                    "subagent_type": "reviewer",
                                },
                                "index": 0,
                            }
                        ],
                    ),
                    {},
                ),
            )

        async def aupdate_state(self, **kw):
            pass

    rows = [e for e in _collect(Agent()) if isinstance(e, ev.SubagentTask)]

    assert len(rows) == 1, rows
    assert rows[0].status == "running"
    assert rows[0].phase_kind == "direct"
    assert rows[0].task_id == "tc_task_1"
    assert rows[0].subagent_type == "reviewer"
    assert rows[0].label == "review auth.py"


def test_async_subagents_appear_at_dispatch_not_at_turn_end():
    """A remote subagent's row is emitted when ``start_async_task`` returns.

    deepagents' launch tool returns ``Command(update={"async_tasks": ...})``, so
    the entry is in the ``updates`` stream at tool time. Ingesting only from the
    Done-time watcher is why these rows used to arrive a whole turn late.
    """

    def entry(status):
        return {
            "task_id": "thread-abc123",
            "agent_name": "code-review-agent",
            "thread_id": "thread-abc123",
            "status": status,
            "created_at": "2026-09-29T10:00:00Z",
        }

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            yield (
                (),
                "updates",
                {"tools": {"messages": [], "async_tasks": {"thread-abc123": entry("running")}}},
            )
            # Every later poll rewrites the same entry; only a status change is news.
            yield ((), "updates", {"tools": {"async_tasks": {"thread-abc123": entry("running")}}})
            yield ((), "updates", {"tools": {"async_tasks": {"thread-abc123": entry("success")}}})

        async def aupdate_state(self, **kw):
            pass

    rows = [e for e in _collect(Agent()) if isinstance(e, ev.SubagentTask)]

    assert [r.status for r in rows] == ["running", "done"], rows
    assert rows[0].task_id == "async:thread-abc123"
    assert rows[0].phase_kind == "async"
    assert rows[0].phase_id == "async"
    assert rows[0].subagent_type == "code-review-agent"
    # Running rows age against the clock; a finished one carries a duration.
    assert rows[0].duration_ms is None
    assert rows[1].duration_ms is not None


if __name__ == "__main__":
    test_happy_path_text_tool_todo()
    test_question_interrupt_resumes()
    test_cancellation_emits_cancelled()
    test_plan_auto_approve_completes_turn()
    test_policy_denied_retry_loop_is_bounded()
    test_user_rejection_does_not_trip_the_guard()
    test_quickjs_custom_events_become_panel_rows()
    test_sync_task_dispatch_becomes_a_panel_row_while_it_runs()
    test_async_subagents_appear_at_dispatch_not_at_turn_end()
    print("ALL TESTS PASSED")


def test_a_subagents_stream_is_previewed_under_its_panel_row():
    """Text, tool calls and results of a subagent carry the row id the task tool stamped."""
    ns = ("tools:abc",)
    meta = {"nova_task_id": "ptc_task_1"}

    class Agent:
        async def aget_state(self, config):
            return _State([])

        async def astream(self, inp, **kw):
            from langchain_core.messages import ToolMessage

            yield (ns, "messages", (_Chunk("m1", [{"type": "text", "text": "Look"}]), meta))
            yield (ns, "messages", (_Chunk("m1", [{"type": "text", "text": "ing"}]), meta))
            call = {"type": "tool_call", "name": "grep", "args": {"pattern": "x"}, "id": "c1"}
            yield (ns, "messages", (_Chunk("m1", [call]), meta))
            result = ToolMessage("Error: bad pattern", tool_call_id="c1", name="grep")
            yield (ns, "messages", (result, meta))
            # Another subagent in the same namespace (a JS fan-out) stays separate.
            other = {"nova_task_id": "ptc_task_2"}
            yield (ns, "messages", (_Chunk("m2", [{"type": "text", "text": "Hi"}]), other))

    previews = [e for e in _collect(Agent()) if isinstance(e, ev.SubagentPreview)]
    assert [(p.task_id, p.kind) for p in previews] == [
        ("ptc_task_1", "text"),
        ("ptc_task_1", "text"),
        ("ptc_task_1", "tool"),
        ("ptc_task_1", "error"),
        ("ptc_task_2", "text"),
    ]
    assert "grep" in previews[2].text
    # Nothing a subagent says leaks into the main transcript.
    assert not [e for e in _collect(Agent()) if isinstance(e, ev.TextDelta)]
