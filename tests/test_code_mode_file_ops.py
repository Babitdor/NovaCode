"""A Code Mode edit gets a diff: the eval call is the only card it has.

A snippet's `tools.call('edit_file', ...)` writes the file, but the nested call
never becomes a message — the eval tool call is the only row the transcript has —
so `file_ops` never recorded a before/after and the TUI never had a diff to
draw. These tests pin the record (tracker), the attribution (parent call id) and
the panel that receives it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.tools import tool

from novacode_cli.agents.code_mode import CodeModeMiddleware
from novacode_cli.file_ops import (
    WRITE_TOOLS,
    FileOpTracker,
    current_file_op_tracker,
    get_session_file_op_tracker,
    reset_session_file_op_tracker,
)
from novacode_cli.security.policy import get_policy
from tests.test_mcp_code_mode import isolated_policy  # noqa: F401
from tests.test_unified_code_mode import invoke, scripted

pytestmark = pytest.mark.timeout(40)


@pytest.fixture(autouse=True)
def _fresh_tracker():
    reset_session_file_op_tracker()
    yield
    reset_session_file_op_tracker()


def _write_tool(path: Path, *, explode: bool = False, new_text: str = "ONE") -> object:
    @tool("edit_file")
    def edit_file(file_path: str, old_string: str, new_string: str) -> str:
        """Replace text in a file."""
        if explode:
            raise ValueError("disk on fire")
        text = Path(file_path).read_text(encoding="utf-8")
        Path(file_path).write_text(text.replace(old_string, new_string), encoding="utf-8")
        return "edited"

    return edit_file


def _call(path: Path, *, old: str = "one", new: str = "ONE") -> str:
    target = str(path).replace("\\", "/")
    return (
        f"await tools.call('edit_file', {{file_path:'{target}', "
        f"old_string:'{old}', new_string:'{new}'}})"
    )


# --- the tracker -----------------------------------------------------------


def test_a_nested_operation_is_attributed_and_taken_once(tmp_path: Path):
    tracker = FileOpTracker(assistant_id="nova-agent")
    target = tmp_path / "app.py"
    target.write_text("one\ntwo\n", encoding="utf-8")

    tracker.start_operation(
        "edit_file", {"file_path": str(target)}, "nested-1", parent_call_id="eval-7"
    )
    target.write_text("ONE\ntwo\n", encoding="utf-8")
    from langchain_core.messages import ToolMessage

    tracker.complete_with_message(ToolMessage(content="edited", tool_call_id="nested-1"))

    records = tracker.take_nested("eval-7")
    assert [r.tool_call_id for r in records] == ["nested-1"]
    assert records[0].parent_call_id == "eval-7"
    assert "-one" in records[0].diff and "+ONE" in records[0].diff
    # Taking them is what stops a replayed message drawing the same diff twice.
    assert tracker.take_nested("eval-7") == []


def test_abandon_drops_an_operation_whose_outcome_is_unknown(tmp_path: Path):
    tracker = FileOpTracker(assistant_id="nova-agent")
    tracker.start_operation("edit_file", {"file_path": str(tmp_path / "x.py")}, "nested-1")
    tracker.abandon("nested-1")
    assert tracker.active == {}
    assert tracker.completed == []


def test_the_current_tracker_is_the_one_the_session_resolved():
    assert current_file_op_tracker() is None
    tracker = get_session_file_op_tracker(assistant_id="nova-agent")
    assert current_file_op_tracker() is tracker
    # A tracker under a different key would resolve paths against another backend.
    get_session_file_op_tracker(assistant_id="nova-server")
    assert current_file_op_tracker() is not tracker
    reset_session_file_op_tracker()
    assert current_file_op_tracker() is None


# --- the code-mode bridge --------------------------------------------------


async def test_a_nested_edit_records_its_diff_under_the_eval_call(
    tmp_path: Path,
    isolated_policy: None,  # noqa: F811 — fixture
):
    target = tmp_path / "app.py"
    target.write_text("one\ntwo\n", encoding="utf-8")
    tracker = get_session_file_op_tracker(assistant_id="nova-agent")
    get_policy().tool_tiers["edit_file"] = "allow"
    graph = scripted(
        [("eval", _call(target))],
        tools=[_write_tool(target)],
        middleware=[CodeModeMiddleware()],
    )

    body = json.loads((await invoke(graph)).content)
    assert body["calls"] == 1
    assert body["failed_calls"] == 0
    assert target.read_text(encoding="utf-8") == "ONE\ntwo\n"

    records = tracker.take_nested("eval_0")
    assert len(records) == 1, "the nested edit was not recorded"
    record = records[0]
    assert record.parent_call_id == "eval_0"
    assert record.tool_name == "edit_file"
    assert record.status == "success"
    assert record.metrics.lines_added == 1
    assert record.metrics.lines_removed == 1
    assert record.diff and "-one" in record.diff and "+ONE" in record.diff


async def test_a_failed_nested_edit_records_nothing(
    tmp_path: Path,
    isolated_policy: None,  # noqa: F811 — fixture
):
    """The write did not land, so a diff would be a guess."""
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")
    tracker = get_session_file_op_tracker(assistant_id="nova-agent")
    get_policy().tool_tiers["edit_file"] = "allow"
    graph = scripted(
        [("eval", _call(target))],
        tools=[_write_tool(target, explode=True)],
        middleware=[CodeModeMiddleware()],
    )

    result = await invoke(graph)
    body = json.loads(result.content)
    assert body["failed_calls"] == 1
    assert "disk on fire" in body["error"]
    assert tracker.take_nested("eval_0") == []
    assert tracker.active == {}


async def test_code_mode_writes_without_a_session_tracker(
    tmp_path: Path,
    isolated_policy: None,  # noqa: F811 — fixture
):
    """No tracker means no diff, never a failure."""
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")
    get_policy().tool_tiers["edit_file"] = "allow"
    graph = scripted(
        [("eval", _call(target))],
        tools=[_write_tool(target)],
        middleware=[CodeModeMiddleware()],
    )

    body = json.loads((await invoke(graph)).content)
    assert body["failed_calls"] == 0
    assert target.read_text(encoding="utf-8") == "ONE\n"
    assert current_file_op_tracker() is None


def test_only_write_tools_are_diffed():
    assert {"write_file", "edit_file"} <= WRITE_TOOLS
    assert "read_file" not in WRITE_TOOLS


# --- the event stream ------------------------------------------------------
# The reducer is what turns a tool message into UI events. A nested edit has no
# message, so it is collected from the tracker and emitted *before* the eval's
# own result — otherwise the panel is already finalized when the diff arrives.


def _drained(*_args: object, **_kwargs: object) -> list:
    """No buffered prose to flush before the events under test."""
    return []


async def test_the_reducer_emits_the_diff_before_the_eval_result(tmp_path: Path):
    from langchain_core.messages import ToolMessage

    from novacode_cli import ui_events as ev
    from novacode_cli.core.agent_loop import _handle_tool_message
    from novacode_cli.core.subagent_tracking import SubagentTracker

    tracker = FileOpTracker(assistant_id="nova-agent")
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")
    tracker.start_operation(
        "edit_file", {"file_path": str(target)}, "nested-1", parent_call_id="eval_0"
    )
    target.write_text("ONE\n", encoding="utf-8")
    tracker.complete_with_message(ToolMessage(content="edited", tool_call_id="nested-1"))

    events = [
        event
        async for event in _handle_tool_message(
            ToolMessage(content='{"result": "edited", "calls": 1}', tool_call_id="eval_0"),
            namespace=None,
            is_main_agent=True,
            is_subagent=False,
            tool_call_to_name={"eval_0": "eval"},
            displayed_tool_ids={"eval_0"},
            seen_message_ids=set(),
            file_op_tracker=tracker,
            subagent_tracker=SubagentTracker(),
            agent_display_name="Nova",
            flush_text=_drained,
        )
    ]

    kinds = [type(event).__name__ for event in events]
    assert kinds == ["FileOp", "ToolResult"], kinds
    file_op = events[0]
    assert isinstance(file_op, ev.FileOp)
    assert file_op.parent_call_id == "eval_0"
    # call_id stays None: no consumer should be told the eval call finished here.
    assert file_op.call_id is None
    assert file_op.record.tool_call_id == "nested-1"


# --- the transcript --------------------------------------------------------
# The eval card is the only row a snippet has, so the diff has to land inside it.


def _panel_text(panel) -> str:
    """The text the panel shows for its details, mounted or not."""
    return panel._output_with_blocks().plain


@pytest.mark.timeout(90)
async def test_a_nested_diff_is_drawn_inside_the_eval_card(tmp_path: Path):
    from novacode_cli import ui_events as ev
    from novacode_cli.file_ops import FileOperationRecord
    from tests.test_tui_home_preferences import _app

    app = _app()
    async with app.run_test(size=(150, 42)) as pilot:
        app._set_home_visible(visible=False)
        await app._render(ev.ToolCall("eval", "await tools.call(...)", "", call_id="eval_0"))
        await pilot.pause()

        record = FileOperationRecord(
            "edit_file",
            "app.py",
            None,
            "nested-1",
            diff="--- app.py\n+++ app.py\n@@ -1 +1 @@\n-one\n+ONE\n",
            parent_call_id="eval_0",
        )
        record.metrics.lines_added = 1
        record.metrics.lines_removed = 1
        await app._render(ev.FileOp(record=record, parent_call_id="eval_0"))
        await pilot.pause()

        panel = app._tool_components["eval_0"][0]
        text = _panel_text(panel)
        assert "app.py" in text
        assert "+1 / -1" in text
        assert "+ONE" in text
        assert "-one" in text
        # The card is still open for its own result: a nested op must not finish it.
        assert "eval_0" in app._tool_components


@pytest.mark.timeout(90)
async def test_only_a_few_diffs_are_drawn_per_eval_card(tmp_path: Path):
    from novacode_cli import ui_events as ev
    from novacode_cli.file_ops import FileOperationRecord
    from novacode_cli.tui import app as app_module
    from tests.test_tui_home_preferences import _app

    app = _app()
    async with app.run_test(size=(150, 42)) as pilot:
        app._set_home_visible(visible=False)
        await app._render(ev.ToolCall("eval", "many edits", "", call_id="eval_0"))
        await pilot.pause()
        for index in range(app_module._CODE_MODE_DIFF_MAX_FILES + 3):
            record = FileOperationRecord(
                "edit_file",
                f"f{index}.py",
                None,
                f"nested-{index}",
                diff=f"--- f{index}.py\n+++ f{index}.py\n@@ -1 +1 @@\n-a\n+b\n",
                parent_call_id="eval_0",
            )
            await app._render(ev.FileOp(record=record, parent_call_id="eval_0"))
        await pilot.pause()

        panel = app._tool_components["eval_0"][0]
        text = _panel_text(panel)
        for index in range(app_module._CODE_MODE_DIFF_MAX_FILES + 3):
            # Every changed file is still named, so nothing goes missing.
            assert f"f{index}.py" in text
        # Past the cap the diff bodies stop; the summary lines do not.
        assert text.count("@@ -1 +1 @@") == app_module._CODE_MODE_DIFF_MAX_FILES


@pytest.mark.timeout(90)
async def test_the_diff_survives_the_eval_result_finishing_the_card():
    """`finish` replaces the panel's output text; the attached diffs stay."""
    from novacode_cli import ui_events as ev
    from novacode_cli.file_ops import FileOperationRecord
    from tests.test_tui_home_preferences import _app

    app = _app()
    async with app.run_test(size=(150, 42)) as pilot:
        app._set_home_visible(visible=False)
        await app._render(ev.ToolCall("eval", "await tools.call(...)", "", call_id="eval_0"))
        await pilot.pause()
        record = FileOperationRecord(
            "edit_file",
            "app.py",
            None,
            "nested-1",
            diff="--- app.py\n+++ app.py\n@@ -1 +1 @@\n-one\n+ONE\n",
            parent_call_id="eval_0",
        )
        await app._render(ev.FileOp(record=record, parent_call_id="eval_0"))
        await pilot.pause()
        panel = app._tool_components["eval_0"][0]  # the result pops the entry
        await app._render(
            ev.ToolResult(
                preview="Ran eval  · 1 call", full_output='{"result": "edited"}', call_id="eval_0"
            )
        )
        await pilot.pause()

        text = _panel_text(panel)
        assert "+ONE" in text, text
        assert '{"result": "edited"}' in text, text
