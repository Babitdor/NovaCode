"""Two async subagent previews must poll at the same time.

The poller used to be ``@work(exclusive=True, group="subagent-preview")``:
opening a second preview cancelled the first one's poller, freezing it at its
last snapshot until it was closed and reopened.
"""

from __future__ import annotations

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual not installed")

from novacode_cli.core import subagent_tasks as st  # noqa: E402
from novacode_cli.tui.screens import SubagentPreviewScreen  # noqa: E402
from novacode_cli.ui_events import SubagentTask  # noqa: E402


class _FakeThreads:
    async def get_state(self, thread_id):
        return {"values": {"messages": [{"type": "ai", "content": f"working on {thread_id}"}]}}


class _FakeClient:
    def __init__(self, url=None, headers=None):
        self.threads = _FakeThreads()


def _async_row(task_id: str, thread: str) -> SubagentTask:
    return SubagentTask(
        task_id=task_id,
        status="running",
        phase_kind="async",
        subagent_type="researcher",
        description=thread,
        started_at=100.0,
    )


async def test_two_async_previews_poll_concurrently(monkeypatch):
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    class _FakeAgent:
        async def aget_state(self, config):
            class _State:
                values = {"messages": []}

            return _State()

    class _SS:
        thread_id = "t1"
        session_id = "s1"
        auto_approve = False
        plan_mode_enabled = False
        todos: list = []
        steering_instructions: list = []

    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="test-model",
    )
    app._home_style = "minimal"

    monkeypatch.setattr(
        "novacode_cli.agents.default_subagents.async_subagents.retrieve_async_subagents",
        lambda: [{"name": "researcher", "url": "http://agent-server", "headers": {}}],
    )
    monkeypatch.setattr("langgraph_sdk.get_client", lambda url=None, headers=None: _FakeClient())

    async with app.run_test(size=(120, 40)) as pilot:
        app._subagent_rows["async:1"] = _async_row("async:1", "thread-1")
        app._subagent_rows["async:2"] = _async_row("async:2", "thread-2")

        app._open_subagent_preview("async:1")
        await pilot.pause()
        first = app.screen
        assert isinstance(first, SubagentPreviewScreen)

        app._open_subagent_preview("async:2")
        await pilot.pause()
        second = app.screen
        assert isinstance(second, SubagentPreviewScreen)
        assert first in app.screen_stack, "the first preview must stay open"

        await pilot.pause(0.3)
        # Both pollers delivered at least their first snapshot: the first
        # preview is not frozen at its opening state.
        assert app._subagent_previews.get("async:1"), "first preview's poller must live"
        assert app._subagent_previews.get("async:2"), "second preview's poller must live"
        assert "working on thread-1" in str(app._subagent_previews["async:1"])
        assert "working on thread-2" in str(app._subagent_previews["async:2"])
