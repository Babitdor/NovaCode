"""Tests for LoopGuardMiddleware — breaking stuck identical-repeat tool loops.

Pins the reported bug: the agent fires the same grep with the same args,
gets "no matches" every time, and never escapes — including when it interleaves
a think/read between the repeats. After ``threshold`` identical calls within the
sliding window the next one is short-circuited, while legitimate retries
(different args, changed result) keep running.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from novacode_cli.tracking.loop_guard import LoopGuardMiddleware


def _request(name: str, args: dict, call_id: str = "c1") -> SimpleNamespace:
    """Minimal ToolCallRequest stand-in (middleware only reads ``tool_call``)."""
    return SimpleNamespace(tool_call={"name": name, "args": args, "id": call_id})


def _msg(text: str, call_id: str = "c1") -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id)


def _run(
    mw: LoopGuardMiddleware, name: str, args: dict, result_text: str
) -> ToolMessage | Command:
    """Drive one sync tool call through the guard, returning what it produced."""
    return mw.wrap_tool_call(_request(name, args), lambda _r: _msg(result_text))


# ── the core loop break ──────────────────────────────────────────────────────


def test_blocks_after_threshold_identical_calls():
    mw = LoopGuardMiddleware(threshold=3)
    args = {"pattern": "morphological|knowledge"}
    # First three identical calls execute normally.
    for _ in range(3):
        out = _run(mw, "grep", args, "no matches")
        assert out.content.startswith("no matches")  # a repeat carries a note
    # The fourth is short-circuited.
    blocked = _run(mw, "grep", args, "no matches")
    assert blocked.status == "error"
    assert "LOOP STOPPED" in blocked.content


def test_blocked_call_does_not_execute_handler():
    mw = LoopGuardMiddleware(threshold=2)
    args = {"q": "x"}
    for _ in range(2):
        _run(mw, "grep", args, "no matches")

    calls: list[int] = []

    def handler(_r: object) -> ToolMessage:
        calls.append(1)
        return _msg("no matches")

    out = mw.wrap_tool_call(_request("grep", args), handler)
    assert out.status == "error"
    assert calls == []  # handler never invoked


# ── false-positive guards ────────────────────────────────────────────────────


def test_different_args_reset_streak():
    mw = LoopGuardMiddleware(threshold=3)
    for _ in range(3):
        _run(mw, "grep", {"q": "a"}, "no matches")
    # Switching the query is real progress — must run, not block.
    out = _run(mw, "grep", {"q": "b"}, "no matches")
    assert out.content.startswith("no matches")  # a repeat carries a note


def test_changed_result_resets_streak():
    mw = LoopGuardMiddleware(threshold=3)
    args = {"q": "a"}
    _run(mw, "grep", args, "no matches")
    _run(mw, "grep", args, "no matches")
    # Same call but now it finds something → streak resets, never blocks.
    _run(mw, "grep", args, "found it!")
    out = _run(mw, "grep", args, "found it!")
    assert out.content.startswith("found it!")


def test_intervening_call_does_not_reset_count():
    mw = LoopGuardMiddleware(threshold=3)
    args = {"q": "a"}
    _run(mw, "grep", args, "no matches")
    _run(mw, "grep", args, "no matches")
    _run(mw, "read_file", {"path": "/x"}, "contents")  # interleaved, skipped
    # Third identical grep is still allowed (only 2 repeats so far)…
    out = _run(mw, "grep", args, "no matches")
    assert out.content.startswith("no matches")  # a repeat carries a note
    # …but the fourth crosses the threshold despite the interleaved read.
    blocked = _run(mw, "grep", args, "no matches")
    assert blocked.status == "error"
    assert "LOOP STOPPED" in blocked.content


def test_blocks_loop_that_alternates_with_another_tool():
    # The reported shape: grep → think → grep → think → grep …
    mw = LoopGuardMiddleware(threshold=3)
    args = {"pattern": "knowledge"}
    for _ in range(3):
        out = _run(mw, "grep", args, "no matches")
        assert out.content.startswith("no matches")  # a repeat carries a note
        _run(mw, "think", {"thought": "still looking"}, "ok")  # interleaved
    blocked = _run(mw, "grep", args, "no matches")
    assert blocked.status == "error"
    assert "LOOP STOPPED" in blocked.content


def test_heavy_interleaving_ages_streak_out_of_window():
    # With a small window, enough unrelated calls between repeats means it is no
    # longer a tight loop — the old repeats age out and the call is allowed.
    mw = LoopGuardMiddleware(threshold=3, window=4)
    args = {"q": "a"}
    _run(mw, "grep", args, "no matches")
    _run(mw, "grep", args, "no matches")
    # Fill the window past capacity with distinct other calls.
    for i in range(4):
        _run(mw, "read_file", {"path": f"/x{i}"}, "contents")
    out = _run(mw, "grep", args, "no matches")  # earlier greps aged out
    assert out.content.startswith("no matches")  # a repeat carries a note


def test_command_result_is_not_guarded():
    mw = LoopGuardMiddleware(threshold=2)
    args = {"q": "a"}
    cmd = Command(update={"messages": []})
    for _ in range(4):
        out = mw.wrap_tool_call(_request("grep", args), lambda _r: cmd)
        assert out is cmd  # never blocked — Command results can't anchor a loop


# ── escalation + hard stop (the "LOOP STOPPED" wall fix) ─────────────────────


def test_successive_blocks_escalate_with_changing_message():
    # Each repeated block must return a *different* message so the model's
    # context changes — that is what perturbs it out of the loop.
    mw = LoopGuardMiddleware(threshold=3, escalate_after=3)
    args = {"q": "a"}
    for _ in range(3):
        _run(mw, "grep", args, "no matches")

    first = _run(mw, "grep", args, "no matches")
    second = _run(mw, "grep", args, "no matches")
    assert first.status == "error"
    assert second.status == "error"
    assert "LOOP STOPPED" in first.content
    assert "LOOP STOPPED" in second.content
    assert first.content != second.content  # escalated, not identical


def test_hard_stops_turn_after_escalation_limit():
    # After escalate_after escalating blocks, the guard ends the turn instead of
    # emitting the wall forever.
    mw = LoopGuardMiddleware(threshold=3, escalate_after=2)
    args = {"q": "a"}
    for _ in range(3):
        _run(mw, "grep", args, "no matches")

    out1 = _run(mw, "grep", args, "no matches")  # block #1
    out2 = _run(mw, "grep", args, "no matches")  # block #2
    out3 = _run(mw, "grep", args, "no matches")  # exceeds escalate_after -> halt
    assert isinstance(out1, ToolMessage)
    assert isinstance(out2, ToolMessage)
    assert "LOOP HALTED" in out3.content
    # The stop itself happens before the next model call; a Command(goto=END)
    # from the tool node never ended the turn (tests/test_loop_guard_halts.py).
    after_the_halt = {"messages": [out3]}
    assert mw.before_model(after_the_halt, None)["jump_to"] == "end"
    assert mw.before_model(after_the_halt, None) is None  # once


def test_real_call_resets_block_escalation():
    # A genuinely different call between blocks counts as progress and resets
    # the escalation streak, so the model isn't punished for recovering.
    mw = LoopGuardMiddleware(threshold=3, escalate_after=2)
    args = {"q": "a"}
    for _ in range(3):
        _run(mw, "grep", args, "no matches")

    _run(mw, "grep", args, "no matches")  # block #1
    # Model breaks out with a different, successful call.
    out = _run(mw, "read_file", {"path": "/x"}, "contents")
    assert out.content == "contents"
    # Streak reset: a later block of the same sig starts at #1 again (not halted).
    blocked = _run(mw, "grep", args, "no matches")
    assert isinstance(blocked, ToolMessage)
    assert "LOOP STOPPED" in blocked.content


# ── async path mirrors sync ──────────────────────────────────────────────────


async def test_async_blocks_after_threshold():
    mw = LoopGuardMiddleware(threshold=3)
    args = {"q": "a"}

    async def handler(_r: object) -> ToolMessage:
        return _msg("no matches")

    for _ in range(3):
        out = await mw.awrap_tool_call(_request("grep", args), handler)
        assert out.content.startswith("no matches")  # a repeat carries a note
    blocked = await mw.awrap_tool_call(_request("grep", args), handler)
    assert blocked.status == "error"
    assert "LOOP STOPPED" in blocked.content
