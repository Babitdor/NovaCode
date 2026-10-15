"""/resume shows the whole conversation, not only what its newest seed holds."""

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


async def test_resume_draws_the_earlier_conversation_in_the_chat():
    import pytest  # noqa: F401 - asyncio mode is configured for the suite
    from textual.widgets import Static

    from tests.test_tui_compaction_row import _app

    sm = _sm()
    first = [HumanMessage("build the TUI with ratatui", id="h1"), AIMessage("Built it.", id="a1")]
    sm.save_session(session_id="s", thread_id="t1", messages=first, assistant_id="nova")
    seed = [SystemMessage("sys", id="sys"), HumanMessage(f"{COMPACTION_SUMMARY_MARKER}\n...", id="sum")]
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
        from novacode_cli.tui.widgets import ChatMessage

        shown = " ".join(
            [m.raw_text for m in app._transcript().query(ChatMessage)]
            + [str(w.render()) for w in app._transcript().query(Static)]
        )
        assert "build the TUI with ratatui" in shown and "Built it." in shown
        assert COMPACTION_SUMMARY_MARKER not in shown
