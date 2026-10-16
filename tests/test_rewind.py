"""/rewind: back to before an earlier message -- conversation, code, or both."""

from __future__ import annotations

import pathlib
import tempfile
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph

from novacode_cli import rewind


def _edit(call_id: str, path: str) -> AIMessage:
    return AIMessage("", tool_calls=[{"id": call_id, "name": "edit_file", "args": {"file_path": path}}])


def test_files_go_back_to_before_the_first_change_and_new_ones_go(monkeypatch):
    monkeypatch.setattr(rewind, "ROOT", pathlib.Path(tempfile.mkdtemp()))
    work = pathlib.Path(tempfile.mkdtemp())
    old = work / "app.py"
    old.write_text("v1")

    rewind.record("t", "c1", old)  # turn 1 edits app.py: v1 -> v2
    old.write_text("v2")
    rewind.record("t", "c2", old)  # turn 2 edits it again: v2 -> v3, and creates new.py
    old.write_text("v3")
    new = work / "new.py"
    rewind.record("t", "c3", new)
    new.write_text("created")

    before_turn_2 = rewind.changes_after("t", {"c2", "c3"})
    assert rewind.restore("t", before_turn_2)
    assert old.read_text() == "v2" and not new.exists()

    rewind.restore("t", rewind.changes_after("t", {"c1"}))
    assert old.read_text() == "v1", "the first record of a path is its state then"


def test_paths_resolve_inside_the_workspace_only():
    work = pathlib.Path(tempfile.mkdtemp())
    assert rewind.physical_path("/src/app.py", work) == (work / "src" / "app.py").resolve()
    assert rewind.physical_path(str(work / "a.py"), work) == (work / "a.py").resolve()
    assert rewind.physical_path("/../../outside.txt", work) is None


def test_only_what_the_user_typed_is_a_rewind_point():
    messages = [
        HumanMessage("build it"),
        HumanMessage("Internal context: skills that may apply."),
        HumanMessage("summary", additional_kwargs={"lc_source": "summarization"}),
        AIMessage("done"),
        HumanMessage("now test it"),
    ]
    assert [text for _, text in rewind.user_turns(messages)] == ["build it", "now test it"]


def test_the_file_tracker_checkpoints_before_an_edit(monkeypatch):
    from novacode_cli.config import config
    from novacode_cli.session.runtime_identity import RuntimeIdentity, current_identity
    from novacode_cli.tracking.file_tracker import FileTrackerMiddleware

    monkeypatch.setattr(rewind, "ROOT", pathlib.Path(tempfile.mkdtemp()))
    work = pathlib.Path(tempfile.mkdtemp())
    (work / "a.py").write_text("before")
    monkeypatch.setattr(config.settings, "get_workspace_root", lambda: work)
    token = current_identity.set(RuntimeIdentity("p", "s", "", "thread-1", "task"))
    try:
        request = SimpleNamespace(tool_call={"name": "edit_file", "id": "c9", "args": {"file_path": "/a.py"}})
        FileTrackerMiddleware()._checkpoint(request)
    finally:
        current_identity.reset(token)
    (work / "a.py").write_text("after")
    rewind.restore("thread-1", rewind.changes_after("thread-1", {"c9"}))
    assert (work / "a.py").read_text() == "before"


def _graph():
    builder = StateGraph(MessagesState)
    builder.add_node("model", lambda state: {})
    builder.add_edge(START, "model")
    return builder.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_rewind_cuts_the_conversation_restores_code_and_refills_the_prompt(monkeypatch):
    from tests.test_tui_compaction_row import _app

    monkeypatch.setattr(rewind, "ROOT", pathlib.Path(tempfile.mkdtemp()))
    work = pathlib.Path(tempfile.mkdtemp())
    target = work / "app.py"
    target.write_text("v1")

    app = _app()
    graph = _graph()
    app.agent = graph
    config = {"configurable": {"thread_id": app.session_state.thread_id}}
    rewind.record(app.session_state.thread_id, "c2", target)  # turn 2's edit
    target.write_text("v2")
    await graph.aupdate_state(config, {"messages": [
        HumanMessage("make the app", id="h1"), AIMessage("made it", id="a1"),
        HumanMessage("now break it", id="h2"), _edit("c2", "/app.py"),
        ToolMessage("ok", tool_call_id="c2", id="t2"), AIMessage("broke it", id="a2"),
    ]}, as_node="model")

    answers = iter([0, 0])  # newest message ("now break it"), then "Conversation and code"

    async def choose(screen):
        return next(answers)

    async def no_save(**kw):
        return None

    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "push_screen_wait", choose)
        monkeypatch.setattr(app, "_save_session", no_save)
        await app._run_rewind("/rewind")
        await pilot.pause(0.2)
        from novacode_cli.tui.widgets import PromptInput

        assert app.query_one("#prompt", PromptInput).text == "now break it"

    kept = (await graph.aget_state(config)).values["messages"]
    assert [m.id for m in kept] == ["h1", "a1"]
    assert target.read_text() == "v1"
