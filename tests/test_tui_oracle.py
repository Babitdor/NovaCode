"""Oracle composer controls preserve drafts and expose owner decisions and threads."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, Switch

from novacode_cli import ui_events as ev
from novacode_cli.core.oracle import OracleRun, OracleSettings, Review, Reviewer
from novacode_cli.tui.app import NovaApp
from novacode_cli.tui.oracle_panel import OraclePanel, OracleSettingsScreen, OracleThreadScreen
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


@pytest.fixture(autouse=True)
def minimal_mount(monkeypatch):
    async def mount(self):
        self._apply_saved_theme()
        self.query_one("#cmdpalette").display = False
        self._root_screen = self.screen
        self._init_root_pane()
        self._set_home_visible(visible=False)
        self._refresh_oracle()

    monkeypatch.setattr(NovaApp, "on_mount", mount)


@pytest.mark.parametrize("size", [(120, 42), (100, 40), (90, 36), (75, 32)])
@pytest.mark.parametrize("design", ["classic", "dashboard", "minimal"])
async def test_toggle_strip_settings_and_threads_preserve_draft(size, design):
    app = _app()
    async with app.run_test(size=size) as pilot:
        app.screen.add_class(f"{design}-ui")
        await pilot.pause()
        code_toggle = app.query_one("#code-mode-toggle", Button)
        oracle_toggle = app.query_one("#oracle-toggle", Button)
        assert code_toggle.parent is oracle_toggle.parent
        assert code_toggle.region.y == oracle_toggle.region.y
        assert code_toggle.region.right == oracle_toggle.region.x
        assert app.screen.region.contains_region(oracle_toggle.region), (
            app.query_one("#composer-actions").classes,
            app.query_one("#composer-actions").region,
            code_toggle.parent.region,
            code_toggle.parent.styles.column_span,
        )
        prompt = app.query_one("#prompt")
        prompt.text = "unfinished draft"
        await pilot.click("#oracle-toggle")
        await pilot.pause()
        assert app.session_state.oracle_enabled
        assert prompt.text == "unfinished draft"
        panel = app.query_one(OraclePanel)
        strip = panel.query_one("#oracle-strip", Button)
        assert strip.display
        assert strip.content_region.height == 1
        assert panel.query_one("#oracle-settings", Button).content_region.height == 1
        assert app.screen.region.contains_region(strip.region)
        run = OracleRun(OracleSettings(), ".", "thread", round=2, status="waiting")
        run.reviewers = [
            Reviewer(
                "first",
                1,
                1,
                "first review input",
                Review(
                    score=5.0,
                    summary="First finding",
                    findings=["Fix code.py"],
                ),
            ),
            Reviewer(
                "final",
                2,
                1,
                "fresh review input",
                Review(
                    score=9.0,
                    summary="Fixed",
                    findings=[],
                ),
            ),
        ]
        run.decision = asyncio.get_running_loop().create_future()
        app.session_state.oracle_run = run
        app._refresh_oracle()
        await pilot.pause()
        assert panel.query_one("#oracle-actions").display
        await pilot.click("#oracle-strip")
        await pilot.pause()
        row = panel.query_one("#oracle-row-1")
        assert "first 5" in str(row.title) and "latest 9" in str(row.title)
        row.collapsed = False
        await pilot.pause()
        thread_button = panel.query_one("#oracle-thread-1", Button)
        assert thread_button.content_region.height == 1
        thread_button.scroll_visible(immediate=True)
        await pilot.pause()
        await pilot.click(thread_button)
        await pilot.pause()
        assert isinstance(app.screen, OracleThreadScreen)
        assert "fresh review input" in " ".join(str(w.content) for w in app.screen.query("Static"))
        await pilot.press("escape")
        await pilot.pause()
        await pilot.click("#oracle-accept")
        await pilot.pause()
        assert run.decision.result()["action"] == "accept"
        assert prompt.text == "unfinished draft"


async def test_settings_validate_cancel_and_save_without_sending(monkeypatch):
    from novacode_cli.config.nova_config import NovaConfig

    saved = []
    monkeypatch.setattr(NovaConfig, "set", lambda self, key, value: saved.append((key, value)))
    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        app.query_one("#prompt").text = "draft"
        await pilot.pause()
        await pilot.click("#oracle-settings")
        await pilot.pause()
        assert isinstance(app.screen, OracleSettingsScreen)
        app.screen.query_one("#oracle-target", Input).value = "11"
        save = app.screen.query_one("#oracle-settings-save", Button)
        save.scroll_visible(immediate=True)
        await pilot.pause()
        await pilot.click(save)
        await pilot.pause()
        assert isinstance(app.screen, OracleSettingsScreen)
        assert not saved
        app.screen.query_one("#oracle-target", Input).value = "9"
        app.screen.query_one("#oracle-rounds", Input).value = "4"
        app.screen.query_one("#oracle-reviewers", Input).value = "3"
        app.screen.query_one("#oracle-default", Switch).value = True
        save.scroll_visible(immediate=True)
        await pilot.pause(0.25)
        await pilot.click(save)
        await pilot.pause()
        assert saved[-1] == (
            "oracle_settings",
            {
                "target": 9.0,
                "max_rounds": 4,
                "reviewers": 3,
                "default_enabled": True,
            },
        )
        assert app.session_state.oracle_settings.target == 9
        assert app.query_one("#prompt").text == "draft"
        await pilot.click("#oracle-settings")
        await pilot.pause()
        await pilot.press("escape")
        assert len(saved) == 1


async def test_live_staleness_and_oracle_interrupt_controls(monkeypatch):
    from novacode_cli.core import oracle

    app = _app()
    async with app.run_test(size=(110, 40)) as pilot:
        await app._run_oracle_mode("/oracle-mode on")
        run = OracleRun(
            OracleSettings(),
            ".",
            app.session_state.thread_id,
            round=1,
            status="passed",
            revision="old",
        )
        app.session_state.oracle_run = run
        monkeypatch.setattr(oracle, "snapshot", lambda _: SimpleNamespace(revision="new"))
        await app._check_oracle_freshness()
        assert run.status == "stale"
        run.decision = asyncio.get_running_loop().create_future()
        event = ev.InterruptRequest("oracle", {"run_id": run.identity}, run.decision)
        handler = asyncio.create_task(app._handle_interrupt(event))
        await pilot.pause()
        await pilot.click("#oracle-pause")
        await asyncio.wait_for(handler, 2)
        assert event.future.result()["action"] == "pause"


def test_new_threads_and_clear_use_saved_default(monkeypatch):
    from novacode_cli.core import oracle
    from novacode_cli.states.Session import SessionState

    monkeypatch.setattr(oracle, "profile_settings", lambda: OracleSettings(default_enabled=True))
    session = SessionState()
    assert session.oracle_enabled
    session.oracle_enabled = False
    session.oracle_run = OracleRun(OracleSettings(), ".", session.thread_id)
    session.reset_conversation()
    assert session.oracle_enabled
    assert session.oracle_run is None


async def test_expanded_oracle_keeps_tabs_and_composer_visible_on_short_home():
    from tests.test_tui_home_preferences import _resize_terminal

    app = _app()
    async with app.run_test(size=(100, 40)) as pilot:
        app._set_home_style("studio")
        app._set_home_visible(visible=True)
        await app._run_oracle_mode("/oracle-mode on")
        panel = app.query_one(OraclePanel)
        panel.query_one("#oracle-drawer").display = True
        await _resize_terminal(pilot, 48, 18)
        assert panel.region.height <= 4, (app.screen.classes, panel.styles.max_height)
        assert app.query_one("#session-tab-bar").region.width == app.size.width
        assert app.screen.region.contains_region(app.query_one("#prompt").region)
        assert app.screen.virtual_size.height <= app.size.height


@pytest.mark.timeout(60)
async def test_real_code_mode_graph_and_oracle_fix_cycle(monkeypatch, tmp_path):
    """Exercise the actual JS bridge, graph stream, workspace grades and renderer."""
    import subprocess

    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from novacode_cli.agent_stream import run_agent_stream
    from novacode_cli.agents.code_mode import CodeModeMiddleware
    from novacode_cli.core import oracle
    from novacode_cli.security import policy
    from novacode_cli.states.Session import SessionState
    from novacode_cli.tui.code_mode_panel import CodeModePanel
    from tests.test_mcp_code_mode import ScriptedModel

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "demo.py"
    path.write_text("def clamp_to_zero(value):\n    return value\n")
    for args in [
        ["init"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "base"],
    ]:
        subprocess.run(["git", "-C", str(workspace), *args], check=True, capture_output=True)

    @tool
    def demo_write(correct: bool) -> str:
        """Write the controlled demo implementation inside the disposable workspace."""
        body = "max(value, 0)" if correct else "abs(value)"
        path.write_text(f"def clamp_to_zero(value):\n    return {body}\n")
        return path.read_text()

    @tool
    def demo_check() -> dict:
        """Check the disposable demo against the required negative-input behavior."""
        passed = "return max(value, 0)" in path.read_text()
        return {"passed": passed, "summary": "1 passed" if passed else "1 failed"}

    local_policy = policy.load_policy(workspace)
    local_policy.tool_tiers.update({"demo_write": "allow", "demo_check": "allow"})
    monkeypatch.setattr(policy, "_CACHED_POLICY", local_policy)
    monkeypatch.setattr("novacode_cli.agent_stream._maybe_verifier", lambda: None)
    messages = []
    for index, correct in enumerate((False, True)):
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"eval-{index}",
                            "name": "MCPCode",
                            "args": {
                                "code": "await tools.call('demo_write', {correct: "
                                + str(correct).lower()
                                + "}); return await tools.call('demo_check');"
                            },
                        }
                    ],
                ),
                AIMessage(
                    content="Fixed and checked." if correct else "Initial draft needs review."
                ),
            ]
        )
    graph = create_agent(
        ScriptedModel(messages=iter(messages), disable_streaming=True),
        tools=[demo_write, demo_check],
        middleware=[CodeModeMiddleware(auto_approve=True)],
        checkpointer=InMemorySaver(),
    )
    state = SessionState(auto_approve=True)
    state.workspace_root = str(workspace)
    state.oracle_enabled = True
    state.oracle_settings = OracleSettings()

    class ReviewModel:
        calls = 0

        def with_structured_output(self, schema):
            assert schema is Review
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            first = self.calls <= 2
            assert "Observed test tool results" in messages[-1].content
            assert ("1 failed" if first else "1 passed") in messages[-1].content
            return {
                "score": 5.0 if first else 9.0,
                "summary": "Negative values return their magnitude." if first else "Fixed.",
                "findings": ["demo.py: return zero for negative values."] if first else [],
            }

    state._model = ReviewModel()
    app = _app()
    events = []
    async with app.run_test(size=(120, 44)) as pilot:
        app.agent, app.session_state = graph, state
        app._refresh_oracle()
        async for event in run_agent_stream("Clamp negative values to zero", graph, "nova", state):
            events.append(event)
            await app._render(event)
        await pilot.pause()
        assert state.oracle_run.status == "passed"
        assert state.oracle_run.round == 2
        assert state.oracle_run.lowest == 9
        assert not state.oracle_run.stale
        assert "return max(value, 0)" in path.read_text()
        assert state._model.calls == 4
        assert len(app.query(CodeModePanel)) == 2
        assert sum(isinstance(e, ev.ToolCall) and e.name == "MCPCode" for e in events) == 2
        assert sum(isinstance(e, ev.Done) for e in events) == 1
        assert all(not r.error for r in state.oracle_run.reviewers)
        assert oracle.snapshot(str(workspace)).revision == state.oracle_run.revision
