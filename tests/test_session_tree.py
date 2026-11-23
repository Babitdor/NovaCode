"""/tree, /export, /share: tree-structured session history."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph

from novacode_cli.session.history_export import export_html, export_json, export_markdown, redact
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


def _reasoning_tree(sm: SessionManager):
    reasoning = AIMessage(
        [{"type": "text", "text": "done"}],
        id="a1",
        additional_kwargs={"reasoning_content": "checked the failing test first"},
    )
    call = AIMessage(
        "", id="c1", tool_calls=[{"id": "t1", "name": "read_file", "args": {"path": "x.py"}}]
    )
    result = ToolMessage("file body", tool_call_id="t1", id="r1")
    tree = sm.history_tree("s")
    tree.sync(_msgs(sm, HumanMessage("fix it", id="h1"), call, result, reasoning))
    return tree


def test_markdown_export_honours_thinking_and_tools(tmp_path: Path):
    sm = SessionManager(tmp_path)
    tree = _reasoning_tree(sm)

    full = export_markdown(tree, title="T", subtitle="s")
    assert "fix it" in full and "done" in full
    assert "checked the failing test first" in full
    assert "read_file" in full and "file body" in full

    no_tools = export_markdown(tree, title="T", subtitle="s", include_tools=False)
    assert "checked the failing test first" in no_tools
    assert "read_file" not in no_tools and "file body" not in no_tools

    no_thinking = export_markdown(tree, title="T", subtitle="s", include_thinking=False)
    assert "checked the failing test first" not in no_thinking
    assert "read_file" in no_thinking

    bare = export_markdown(
        tree, title="T", subtitle="s", include_thinking=False, include_tools=False
    )
    assert "read_file" not in bare and "file body" not in bare
    assert "checked the failing test first" not in bare
    assert "fix it" in bare and "done" in bare


def test_markdown_export_follows_the_current_path_only(tmp_path: Path):
    sm = SessionManager(tmp_path)
    tree = sm.history_tree("s")
    tree.sync(_msgs(sm, HumanMessage("first", id="h1"), AIMessage("kept", id="a1")))
    tree.sync(_msgs(sm, HumanMessage("first", id="h1"), HumanMessage("fork", id="h2")))
    md = export_markdown(tree, title="T", subtitle="s")
    assert "first" in md and "fork" in md
    assert "kept" not in md, "the abandoned branch is not on the current path"


def test_json_export_honours_thinking_and_tools(tmp_path: Path):
    sm = SessionManager(tmp_path)
    tree = _reasoning_tree(sm)

    data = json.loads(export_json(tree, title="T", subtitle="s"))
    assert data["format"] == "nova-session-export/1"
    assert data["options"] == {"include_thinking": True, "include_tools": True}
    assert [m["role"] for m in data["messages"]] == ["user", "tool", "tool", "assistant"]
    assistant = data["messages"][-1]
    assert assistant["thinking"] == "checked the failing test first"
    assert "tool_result" not in assistant
    call_record = data["messages"][1]
    assert call_record["tool_calls"] == [{"name": "read_file", "args": {"path": "x.py"}}]
    assert data["messages"][2]["tool_result"] == {"name": "read_file", "text": "file body"}

    bare = json.loads(
        export_json(tree, title="T", subtitle="s", include_thinking=False, include_tools=False)
    )
    assert all("thinking" not in m and "tool_calls" not in m for m in bare["messages"])
    assert [m["role"] for m in bare["messages"]] == ["user", "assistant"], (
        "tool nodes drop out when tools are excluded"
    )


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
async def test_export_dialog_answers_format_and_inclusions():
    from textual.app import App

    from novacode_cli.tui.screens import ExportOptionsModal

    app = App()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = ExportOptionsModal()
        shown: list = []
        app.push_screen(screen, shown.append)
        await pilot.pause()

        assert screen.result() == {"format": "markdown", "thinking": True, "tools": True}

        await pilot.click("#fmt-json")
        await pilot.click("#inc-tools")
        assert screen.result() == {"format": "json", "thinking": False, "tools": True}

        await pilot.click("#inc-none")
        assert screen.result() == {"format": "json", "thinking": False, "tools": False}

        await pilot.click("#export")
        await pilot.pause()
        assert shown and shown[0] == {"format": "json", "thinking": False, "tools": False}

        screen2 = ExportOptionsModal()
        app.push_screen(screen2, shown.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert shown[-1] is None, "escape cancels the export"


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

        out = tmp_path / "out.md"
        monkeypatch.setattr(
            app,
            "push_screen_wait",
            AsyncMock(return_value={"format": "markdown", "thinking": True, "tools": True}),
        )
        await app._run_export(f"/export {out}")
        page = out.read_text(encoding="utf-8")
        assert "made it" in page, "the current path exports as Markdown"
        assert "add a logo" not in page, "the abandoned branch is not on the current path"
        # The whole tree stays available through the HTML page /share builds.
        assert "add a logo" in app._export_page(
            app.session_manager.history_tree(app.session_state.session_id)
        )

        out_json = tmp_path / "out.json"
        monkeypatch.setattr(
            app,
            "push_screen_wait",
            AsyncMock(return_value={"format": "json", "thinking": False, "tools": False}),
        )
        await app._run_export(f"/export {out_json}")
        data = json.loads(out_json.read_text(encoding="utf-8"))
        assert data["format"] == "nova-session-export/1"
        assert [m["role"] for m in data["messages"]] == ["user", "assistant"]


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
