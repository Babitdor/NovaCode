"""/resume opens at the last compaction, as Claude Code does; the rest stays on disk."""

import pathlib
import tempfile

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from novacode_cli.compaction import COMPACTION_SUMMARY_MARKER
from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.tui.app import replay_window


def _sm() -> SessionManager:
    return SessionManager(sessions_dir=pathlib.Path(tempfile.mkdtemp()))


def test_a_new_conversation_under_the_session_keeps_the_old_one():
    """A /resume (or a plan run) seeds a fresh thread with only a summary."""
    sm = _sm()
    first = [HumanMessage("build the TUI", id="h1"), AIMessage("done: ratatui app", id="a1")]
    sm.save_session(session_id="s", thread_id="t1", messages=first, assistant_id="nova")
    sm.save_session(session_id="s", thread_id="t1", messages=first, assistant_id="nova")
    assert sm.load_history("s") == [], "the same conversation saved again keeps nothing extra"

    seed = [SystemMessage("sys", id="sys"), HumanMessage(f"{COMPACTION_SUMMARY_MARKER}\n...", id="sum")]
    sm.save_session(session_id="s", thread_id="t2", messages=seed, assistant_id="nova")

    kept = sm.load_history("s")
    assert [m.id for m in kept] == ["h1", "a1"]
    assert [m.id for m in sm.load_session("s").messages] == ["sys", "sum"], "the agent still gets the seed"

    sm.save_session(session_id="s", thread_id="t3", messages=[HumanMessage("x", id="h3")], assistant_id="nova")
    assert [m.id for m in sm.load_history("s")] == ["h1", "a1", "sys", "sum"], "segments accumulate"


def test_a_cleared_save_keeps_no_history():
    sm = _sm()
    sm.save_session(session_id="s", thread_id="t1", messages=[HumanMessage("a", id="h")], assistant_id="nova")
    sm.save_session(session_id="s", thread_id="t2", messages=[], assistant_id="nova", cleared=True)
    assert sm.load_history("s") == []


def test_replay_fits_by_what_is_drawn_not_by_message_count():
    msgs = []
    for turn in range(30):
        msgs.append(HumanMessage(f"turn {turn}", id=f"h{turn}"))
        for k in range(10):  # ten tool calls a turn: one drawn group
            call = {"id": f"c{turn}-{k}", "name": "read_file", "args": {}, "type": "tool_call"}
            msgs.append(AIMessage("", tool_calls=[call], id=f"a{turn}-{k}"))
            msgs.append(ToolMessage("x", tool_call_id=f"c{turn}-{k}", id=f"t{turn}-{k}"))
        msgs.append(AIMessage(f"answer {turn}", id=f"r{turn}"))
    # 630 messages, 3 drawn items a turn: all 30 turns fit 190 items.
    assert replay_window(msgs, 190)[0].id == "h0"
    window = replay_window(msgs, 30)
    assert window[0].type == "human", "never opens mid-turn"
    assert 9 <= sum(m.type == "human" for m in window) <= 10
    assert window[-1].id == "r29"


async def test_resume_opens_at_the_last_compaction():
    import pytest  # noqa: F401 - asyncio mode is configured for the suite
    from textual.widgets import Collapsible

    from tests.test_tui_compaction_row import _app
    from novacode_cli.tui.widgets import ChatMessage

    sm = _sm()
    first = [HumanMessage("build the TUI with ratatui", id="h1"), AIMessage("Built it.", id="a1")]
    sm.save_session(session_id="s", thread_id="t1", messages=first, assistant_id="nova")
    seed = [
        SystemMessage("sys", id="sys"),
        HumanMessage(
            f"{COMPACTION_SUMMARY_MARKER}\n\nThe TUI was built.",
            id="sum",
            additional_kwargs={"lc_source": "summarization"},
        ),
        HumanMessage("now add tests", id="h2"),
        AIMessage("Added them.", id="a2"),
    ]
    sm.save_session(session_id="s", thread_id="t2", messages=seed, assistant_id="nova")

    app = _app()
    app.session_manager = sm
    app.session_state.session_id = "s"
    async with app.run_test(size=(110, 40)) as pilot:
        await app._transcript().remove_children()
        app._restored_messages = sm.load_session("s").messages  # what /resume restores
        app._replay_history()
        for _ in range(20):
            await pilot.pause(0.05)
        said = " ".join(m.raw_text for m in app._transcript().query(ChatMessage))
        assert "now add tests" in said and "Added them." in said
        assert "build the TUI with ratatui" not in said, "what the summary replaced is not drawn"
        card = app._transcript().query(Collapsible).first()
        assert "compacted" in str(card.title) and card.collapsed


def test_a_resume_that_compacts_seeds_the_whole_conversation():
    """Its newest ~30k tokens are not what a summary of the session needs."""
    from types import SimpleNamespace

    from novacode_cli.session.session_prompt_builder import build_continuation_prompt

    old = [HumanMessage("x" * 9_000, id=f"h{i}") for i in range(40)]  # ~120k tokens
    data = SimpleNamespace(memory=None, messages=old, meta=SimpleNamespace(current_task=None, task_status=None, blocked_reason=None, next_step_hint=None), todos=[])
    trimmed = build_continuation_prompt(data, "sys")
    whole = build_continuation_prompt(data, "sys", keep_all=True)
    assert len(trimmed) < 41
    assert [m.id for m in whole[1:]] == [m.id for m in old]
