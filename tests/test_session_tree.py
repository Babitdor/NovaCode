"""/tree, /export, /share: tree-structured session history."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph

from novacode_cli.session.history_export import export_html, redact
from novacode_cli.session.history_tree import HistoryTree
from novacode_cli.session.session_persistence import SessionManager

if TYPE_CHECKING:
    from pathlib import Path


def _msgs(manager: SessionManager, *messages) -> list[dict]:
    return [manager._serialize_message(m) for m in messages]


def test_continuing_from_an_earlier_point_grows_a_branch(tmp_path: Path):
    sm = SessionManager(tmp_path)
    h1, a1 = HumanMessage("fix login", id="h1"), AIMessage("fixed", id="a1")
    h2, a2 = HumanMessage("add tests", id="h2"), AIMessage("added", id="a2")
    h3 = HumanMessage("use oauth instead", id="h3")
    tree = sm.history_tree("s")
    tree.sync(_msgs(sm, h1, a1, h2, a2))
    tree.sync(_msgs(sm, h1, a1))  # went back
    tree.sync(_msgs(sm, h1, a1, h3))  # and continued elsewhere
    assert tree.nodes["a1"].children == ["h2", "h3"]
    assert tree.leaf == "h3"
    sm.label_history("s", "a2", "tests done")

    again = HistoryTree(tmp_path / "s")  # everything comes back from the one file
    assert set(again.nodes) == {"h1", "a1", "h2", "a2", "h3"}
    assert again.leaf == "h3"
    assert again.nodes["a2"].label == "tests done"
    assert [n.id for n in again.path_to("a2")] == ["h1", "a1", "h2", "a2"]
    lines = (tmp_path / "s" / "tree.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum(json.loads(ln)["t"] == "node" for ln in lines) == 5, "nodes are written once"


def test_a_torn_last_line_is_ignored(tmp_path: Path):
    sm = SessionManager(tmp_path)
    sm.history_tree("s").sync(_msgs(sm, HumanMessage("hi", id="h1")))
    with (tmp_path / "s" / "tree.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"t": "node", "id": "x", "par')
    assert set(HistoryTree(tmp_path / "s").nodes) == {"h1"}


def test_continue_points_never_leave_a_tool_call_unanswered(tmp_path: Path):
    sm = SessionManager(tmp_path)
    call = AIMessage("", id="c1", tool_calls=[{"id": "t1", "name": "read_file", "args": {}}])
    tree = sm.history_tree("s")
    tree.sync(
        _msgs(
            sm,
            HumanMessage("read it", id="h1"),
            call,
            ToolMessage("contents", tool_call_id="t1", id="r1"),
            AIMessage("it says hi", id="a1"),
            HumanMessage("thanks", id="h2"),
        )
    )
    assert [n.id for n in tree.continue_point("a1")[0]] == ["h1", "c1", "r1", "a1"]
    path, prompt = tree.continue_point("h2")
    assert [n.id for n in path] == ["h1", "c1", "r1", "a1"]
    assert prompt == "thanks"
    path, prompt = tree.continue_point("r1")  # a tool step resolves upward
    assert path == [] and prompt == "read it"


def test_the_export_holds_every_branch_and_masks_secrets(tmp_path: Path):
    sm = SessionManager(tmp_path)
    tree = sm.history_tree("s")
    tree.sync(_msgs(sm, HumanMessage("key sk-abcdefghijklmnopqrstuvwx </script>", id="h1")))
    tree.sync(_msgs(sm, HumanMessage("other branch", id="h9")))
    page = export_html(tree, title="T <b>", subtitle="")
    assert "sk-abcdefghijklmnopqrstuvwx" not in page
    assert "[redacted]" in page
    assert "other branch" in page
    assert "T &lt;b&gt;" in page
    data = page.split('id="data">', 1)[1].split("</script>", 1)[0]
    assert set(json.loads(data)["nodes"]) == {"h1", "h9"}, "the embedded JSON stays intact"
    assert redact("ghp_" + "a" * 36) == "[redacted]"


def _graph():
    builder = StateGraph(MessagesState)
    builder.add_node("model", lambda state: {})
    builder.add_edge(START, "model")
    return builder.compile(checkpointer=InMemorySaver())


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novacode_cli.config import config
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated")
async def test_tree_moves_the_conversation_and_export_writes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from novacode_cli.tui.history_tree_screen import HistoryTreeScreen
    from novacode_cli.tui.widgets import PromptInput
    from tests.test_tui_sessions import _app

    app = _app()
    graph = _graph()
    app.agent = graph
    app.session_manager = SessionManager(tmp_path / "sessions")
    config = {"configurable": {"thread_id": app.session_state.thread_id}}
    await graph.aupdate_state(
        config,
        {
            "messages": [
                HumanMessage("make the app", id="h1"),
                AIMessage("made it", id="a1"),
                HumanMessage("now style it", id="h2"),
                AIMessage("styled", id="a2"),
            ]
        },
        as_node="model",
    )
    picks = iter(["h2", "a1"])
    screens = []

    async def choose(screen):
        screens.append(screen)
        return next(picks)

    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "push_screen_wait", choose)
        await app._run_tree()
        await pilot.pause(0.2)
        assert isinstance(screens[0], HistoryTreeScreen)
        assert app.query_one("#prompt", PromptInput).text == "now style it"
        assert [m.id for m in (await graph.aget_state(config)).values["messages"]] == ["h1", "a1"]

        # Continue down a new branch, then come back to the answer it forked from.
        await graph.aupdate_state(
            config, {"messages": [HumanMessage("add a logo", id="h3")]}, as_node="model"
        )
        await app._run_tree()
        await pilot.pause(0.2)
        tree = app.session_manager.history_tree(app.session_state.session_id)
        assert tree.nodes["a1"].children == ["h2", "h3"]
        assert tree.leaf == "a1"
        assert app.query_one("#prompt", PromptInput).text == ""

        out = tmp_path / "out.html"
        await app._run_export(f"/export {out}")
        page = out.read_text(encoding="utf-8")
        assert "now style it" in page and "add a logo" in page


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated")
async def test_tree_screen_filters_bookmarks_and_picks(tmp_path: Path):
    from textual.app import App
    from textual.widgets import Input, Tree

    from novacode_cli.tui.history_tree_screen import HistoryTreeScreen

    sm = SessionManager(tmp_path)
    tree = sm.history_tree("s")
    call = AIMessage("", id="c1", tool_calls=[{"id": "t1", "name": "read_file", "args": {}}])
    tree.sync(_msgs(sm, HumanMessage("one", id="h1"), call, AIMessage("done", id="a1")))
    tree.sync(_msgs(sm, HumanMessage("one", id="h1"), HumanMessage("two", id="h2")))
    labels = []
    results = []
    app = App()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = HistoryTreeScreen(tree, lambda nid, text: labels.append((nid, text)))
        app.push_screen(screen, results.append)
        await pilot.pause()
        widget = screen.query_one(Tree)

        def shown() -> list[str]:
            out, todo = [], list(widget.root.children)
            while todo:
                node = todo.pop(0)
                out.append(node.data)
                todo[:0] = node.children
            return out

        assert shown() == ["h1", "c1", "a1", "h2"], "fork heads show even when filtered"
        await pilot.press("f")  # all
        assert "c1" in shown()
        await pilot.press("f", "f")  # user, then bookmarks
        await pilot.pause()
        assert screen._filter == "bookmarks"
        assert widget.cursor_node.data == "h2", "the cursor opens on the current message"
        await pilot.press("b")
        await pilot.press(*"logo")
        await pilot.press("enter")
        assert labels == [("h2", "logo")]
        assert not screen.query_one(Input).has_class("editing")
        await pilot.press("enter")
        await pilot.pause()
        assert results == ["h2"]
