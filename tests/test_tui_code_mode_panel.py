"""Code Mode's compact presentation must retain source, errors and selected output."""

import json
from pathlib import Path

import pytest
from textual.widgets import Collapsible

from novacode_cli.tui.code_mode_panel import CodeModePanel, summarize_run


def test_success_title_is_only_the_recorded_tool_names():
    title, _, failed = summarize_run(
        json.dumps(
            {
                "calls": 4,
                "failed_calls": 0,
                "outcomes": [{"tool_name": "read_file", "status": "completed"}] * 4,
            }
        ),
        is_error=False,
        preview="done",
    )
    assert title == "read_file x4"
    assert not failed


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
    assert "Code Mode" not in title
    assert "calls" not in title
    assert "failed" not in title
    assert "read_file x2" in title
    assert "tools.list" in title
    assert "1 operation records omitted" in note
    assert "File not found" in note
    assert "Do not replay" in note
    assert "truncated" in note


@pytest.mark.parametrize(
    "output", ["not JSON", "null", "[]", "{", '{"outcomes":[null,3,{"status":[]}],"calls":"many"}']
)
def test_unknown_output_stays_inspectable_without_inventing_counts(output: str):
    title, note, failed = summarize_run(output, is_error=True, preview="Execution failed")
    assert failed
    assert title == "eval"
    assert " calls" not in title
    assert "Execution failed" in note


@pytest.mark.asyncio
async def test_eval_events_create_keyboard_accessible_panels_and_keep_ids(  # noqa: PLR0915
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
        assert panels[1].collapsed
        assert panels[0].heading.render().plain == "Ran read_file"
        assert panels[1].heading.render().plain == "Failed MCPCode"
        assert panels[1].has_class("failed")
        assert panels[0].source is panels[0].output is None
        assert "limit must be <= 50" in panels[1].preview.render().plain
        assert "Inspect completed" in str(panels[1].note.render())
        assert panels[0].styles.border_top[0] in {"", "none"}
        assert panels[1].styles.border_top[0] in {"", "none"}
        assert panels[0].preview.display
        assert not panels[0].note.display
        from novacode_cli.tui.themes import NOVA_EXTRA_THEMES

        for theme in NOVA_EXTRA_THEMES[:3]:
            app.register_theme(theme)
        app.theme = "nova-classic"
        await pilot.pause()
        first_status = panels[0].status.styles.color
        app.theme = "nova-synthwave"
        await pilot.pause()
        assert panels[0].status.styles.color != first_status
        assert all(c.collapsed for c in panels[1].query(Collapsible))
        assert not app._tool_components
        # A keyboard user can open the run and then its source disclosure.
        panels[0].query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not panels[0].collapsed
        assert source in panels[0].source.render().plain
        assert panels[0].output.render().plain == good
        nested = list(panels[0].query(Collapsible))
        nested[0].query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not nested[0].collapsed
        # Code Mode envelopes bypass the generic 6,000-character error preview cap.
        await app._render(
            ev.ToolCall(name="eval", display_str="eval()", icon="*", args={}, call_id="large")
        )
        large = json.dumps(
            {
                "result": "x" * 9000 + "exact-tail",
                "truncated": True,
                "outcomes": [{"tool_name": "execute", "status": "completed"}],
                "output_file": {"path": "/cleared/selected.json"},
            }
        )
        await app._render(ev.ToolResult(preview="done", full_output=large, call_id="large"))
        large_panel = list(app.query(CodeModePanel))[-1]
        assert large_panel.heading.render().plain == "Ran execute"
        assert large_panel.output is None
        assert "/cleared/selected.json" in large_panel.note.render().plain
        large_panel.collapsed = False
        await pilot.pause()
        assert large_panel.output.render().plain == large
        # ID-less legacy events use the most recent Code Mode component.
        await app._render(ev.ToolCall(name="eval", display_str="eval()", icon="*", args={}))
        await app._render(ev.ToolResult(preview="done", full_output='{"result":42,"calls":0}'))
        assert list(app.query(CodeModePanel))[-1].heading.render().plain == "Ran eval"
        # Normal tools share the compact presentation.
        await app._render(
            ev.ToolCall(name="read_file", display_str="read_file(a.py)", icon="*", call_id="read")
        )
        normal = list(app.query(CodeModePanel))[-1]
        assert normal.heading.render().plain == "Running read_file(a.py)"
        assert normal.collapsed
        assert normal.styles.border_top[0] in {"", "none"}
        await app._render(
            ev.ToolResult(
                preview="done",
                full_output='{"error":"ordinary data","result":42}',
                call_id="read",
            )
        )
        assert normal.heading.render().plain == "Ran read_file(a.py)"
        assert not normal.has_class("failed")
        assert normal.output is None
        normal.collapsed = False
        await pilot.pause()
        assert '"error":"ordinary data"' in normal.output.render().plain
        assert normal.query_one(Collapsible).title == "Arguments"
        await app._render(
            ev.ToolCall(
                name="execute",
                display_str="execute(echo hello)",
                icon="*",
                args={"command": "echo hello", "shell": "native"},
                call_id="shell",
            )
        )
        shell = list(app.query(CodeModePanel))[-1]
        assert not shell.collapsed
        app._write_tool_output("shell", "hello [bold]live[/bold]")
        await pilot.pause()
        assert "hello [bold]live[/bold]" in "\n".join(line.text for line in shell.output.lines)
        await app._render(
            ev.ToolResult(
                preview="Exited 1",
                full_output="failed [literal]\nline2\nline3\nline4",
                is_error=True,
                call_id="shell",
            )
        )
        await pilot.pause()
        assert shell.has_class("failed")
        assert not shell.has_class("tool-active")
        assert "line4" not in shell.preview.render().plain
        assert "line4" in "\n".join(line.text for line in shell.output.lines)
        assert not shell.query_one(".background-shell-hint").display
        assert not app._tool_components
        await app._render(
            ev.ToolCall(
                name="read_file",
                display_str="read_file(legacy.py)",
                icon="*",
            )
        )
        legacy = list(app.query(CodeModePanel))[-1]
        await app._render(ev.FileOp(record=None, full_output="legacy content"))
        assert legacy.heading.render().plain == "Ran read_file(legacy.py)"
        legacy.collapsed = False
        await pilot.pause()
        assert "legacy content" in legacy.output.render().plain
        assert app._last_tool is None


@pytest.mark.asyncio
async def test_compact_result_preview_keeps_full_output_source_and_recovery():
    from textual.app import App, ComposeResult

    class Preview(App):
        def compose(self) -> ComposeResult:
            yield CodeModePanel("// exact [source]\nreturn result;")

    app = Preview()
    async with app.run_test(size=(64, 24)) as pilot:
        panel = app.query_one(CodeModePanel)
        result = "one\ntwo\n[bold]three[/bold]\nfour\nfive"
        output = json.dumps(
            {"result": result, "outcomes": [{"tool_name": "execute", "status": "completed"}]}
        )
        panel.finish(output, is_error=False, preview="done")
        await pilot.pause()
        assert panel.heading.render().plain == "Ran execute"
        assert panel.preview.render().plain == "└ one\n  two\n  [bold]three[/bold]"
        assert "+ 2 lines" in str(panel.title)
        assert panel.collapsed
        assert "five" not in panel.preview.render().plain
        assert panel.source is panel.output is None
        assert panel.unmounted_detail_chars >= len(output)
        panel.query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not panel.collapsed
        assert panel.output.render().plain == output
        assert panel.source.render().plain == "// exact [source]\nreturn result;"
        saved = "/cleared/selected.json"
        panel.finish(
            json.dumps({"result": "prefix", "truncated": True, "output_file": {"path": saved}}),
            is_error=False,
            preview="done",
        )
        await pilot.pause()
        assert panel.note.display
        assert saved in panel.note.render().plain
        assert not panel.collapsed  # A finishing event retains the user's disclosure state.
