"""Code Mode's compact presentation must retain source, errors and selected output."""

import json
from pathlib import Path

import pytest
from textual.widgets import Collapsible

from novacode_cli.tui.code_mode_panel import CodeModePanel, summarize_run


def test_summary_uses_recorded_calls_and_marks_caught_failures():
    output = json.dumps(
        {
            "calls": 4,
            "failed_calls": 1,
            "outcomes_omitted": 1,
            "result": "selected data",
            "outcomes": [
                {"namespace": "tools", "action": "list", "status": "success"},
                {"tool_name": "read_file", "status": "success"},
                {
                    "tool_name": "read_file",
                    "status": "failed",
                    "error": {"message": "File not found"},
                },
            ],
            "recovery": "Do not replay completed calls.",
            "truncated": True,
        }
    )
    title, note, failed = summarize_run(output, is_error=False, preview="")
    assert failed
    assert "4 calls" in title
    assert "read_file x2" in title
    assert "tools.list" in title
    assert "1 records omitted" in title
    assert "File not found" in note
    assert "Do not replay" in note
    assert "truncated" in note


@pytest.mark.parametrize(
    "output", ["not JSON", "null", "[]", "{", '{"outcomes":[null,3,{"status":[]}],"calls":"many"}']
)
def test_unknown_output_stays_inspectable_without_inventing_counts(output: str):
    title, note, failed = summarize_run(output, is_error=True, preview="Execution failed")
    assert failed
    assert "failed" in title
    assert " calls" not in title
    assert "Execution failed" in note


@pytest.mark.asyncio
async def test_eval_events_create_keyboard_accessible_panels_and_keep_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import novacode_cli.ui_events as ev
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    # Only mount the real widgets; no services, user history, or model work.
    async def mount(self: NovaApp) -> None:
        self._apply_saved_theme()
        self.query_one("#cmdpalette").display = False
        self._root_screen = self.screen
        self._init_root_pane()
        self._set_home_visible(visible=False)

    monkeypatch.setenv("NOVA_HOME", str(tmp_path))
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setattr(NovaApp, "on_mount", mount)
    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="demo-model",
        model_provider="demo",
    )
    source = "const value = await tools.call('read_file', {file_path:'/parser.py'}); value"
    async with app.run_test(size=(80, 30)) as pilot:
        for name, call_id in [("eval", "a"), ("MCPCode", "b")]:
            await app._render(
                ev.ToolCall(
                    name=name,
                    display_str="eval(code=raw snippet)",
                    icon="*",
                    args={"code": source},
                    call_id=call_id,
                )
            )
        await pilot.pause()
        panels = list(app.query(CodeModePanel))
        assert len(panels) == 2
        assert all(p.collapsed for p in panels)
        good = json.dumps(
            {
                "result": "literal [bold] text",
                "calls": 1,
                "outcomes": [{"tool_name": "read_file", "status": "success"}],
            }
        )
        bad = json.dumps(
            {
                "error": "ValidationError: limit must be <= 50",
                "calls": 0,
                "hint": "Inspect completed calls before retrying.",
            }
        )
        # Results arrive out of order: each must update its own panel.
        await app._render(
            ev.ToolResult(preview="failed", full_output=bad, is_error=True, call_id="b")
        )
        await app._render(ev.ToolResult(preview="done", full_output=good, call_id="a"))
        await pilot.pause()
        assert panels[0].collapsed
        assert not panels[1].collapsed
        assert "read_file" in str(panels[0].title)
        assert "failed" in str(panels[1].title)
        assert source in panels[0].source.render().plain
        assert "limit must be <= 50" in str(panels[1].note.render())
        assert "[bold]" in panels[0].output.render().plain
        assert all(c.collapsed for c in panels[1].query(Collapsible))
        assert not app._tool_components
        # A keyboard user can open the run and then its source disclosure.
        panels[0].query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not panels[0].collapsed
        nested = list(panels[0].query(Collapsible))
        nested[0].query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not nested[0].collapsed
        # ID-less legacy events use the most recent Code Mode component.
        await app._render(ev.ToolCall(name="eval", display_str="eval()", icon="*", args={}))
        await app._render(ev.ToolResult(preview="done", full_output='{"result":42,"calls":0}'))
        assert "complete" in str(list(app.query(CodeModePanel))[-1].title)
        # Other tools keep their existing condensed group.
        await app._render(
            ev.ToolCall(name="read_file", display_str="read_file(a.py)", icon="*", call_id="read")
        )
        assert app._tool_group_entries[-1]["category"] == "Explored"
