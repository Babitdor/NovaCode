"""``/trace`` in the TUI asks for the LangSmith environment and activates it.

The command has to do two things at once: take the four ``LANGSMITH_*``
variables from the user, and make LangSmith pick them up although the
process was started long before the values were entered.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from textual.widgets import Button, Input, Select

from novacode_cli.tracking.tracing import (
    LANGSMITH_ENV_VARS,
    get_tracing_config,
    reset_langsmith_caches,
)


class _StateVal:
    def __init__(self, msgs):
        self.values = {"messages": msgs}


class _FakeAgent:
    async def aget_state(self, config):
        return _StateVal([])

    async def astream(self, inp, **kw):
        return
        yield  # pragma: no cover - the trace command never calls the agent


class _SS:
    thread_id = "t1"
    auto_approve = True
    plan_mode_enabled = False
    todos: list = []
    steering_instructions: list = []


def _app():
    """A NovaApp with just enough state to run a slash command."""
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
    )


@pytest.fixture
def clean_langsmith_env(monkeypatch, tmp_path):
    """Restore the LangSmith environment the test changed."""
    import novacode_cli.config.config as config_mod
    from novacode_cli.tracking import tracing

    class StubSecretManager:
        store = {}

        def __init__(self):
            pass

        def get_secret(self, name):
            return self.store.get(name)

        def store_secret(self, name, value):
            self.store[name] = value
            return True

        def delete_secret(self, name):
            self.store.pop(name, None)
            return True

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", StubSecretManager)
    monkeypatch.setattr(tracing, "_tracing_config", tracing.TracingConfig())
    saved = {name: os.environ.get(name) for name in LANGSMITH_ENV_VARS}
    for name in LANGSMITH_ENV_VARS:
        os.environ.pop(name, None)
    reset_langsmith_caches()
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    reset_langsmith_caches()


async def test_trace_setup_asks_for_the_environment_and_activates_tracing(
    clean_langsmith_env,
):
    app = _app()
    async with app.run_test() as pilot:
        # Slash commands run in a worker, and the modal needs that context.
        worker = app.run_worker(app._run_trace("/trace setup"), group="test")
        await pilot.pause()

        modal = app.screen
        modal.query_one("#langsmith-tracing", Select).value = "true"
        modal.query_one("#langsmith-api-key", Input).value = "lsv2_test"
        modal.query_one("#langsmith-project", Input).value = "Nova-Test"
        modal.query_one("#langsmith-endpoint", Input).value = "https://example.test"
        await pilot.pause()
        modal.query_one("#do-save", Button).press()
        await pilot.pause()
        await asyncio.wait_for(worker.wait(), timeout=5)

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_test"
    assert os.environ["LANGSMITH_PROJECT"] == "Nova-Test"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://example.test"

    config = get_tracing_config()
    assert config.is_configured()
    assert config.project_name == "Nova-Test"
    assert config.endpoint == "https://example.test"


async def test_trace_status_reports_the_configured_state(clean_langsmith_env):
    """``/trace status`` must show the state the setup flow left behind."""
    app = _app()
    async with app.run_test() as pilot:
        worker = app.run_worker(app._run_trace("/trace setup"), group="test")
        await pilot.pause()
        modal = app.screen
        modal.query_one("#langsmith-tracing", Select).value = "true"
        modal.query_one("#langsmith-api-key", Input).value = "lsv2_test"
        await pilot.pause()
        modal.query_one("#do-save", Button).press()
        await pilot.pause()
        await asyncio.wait_for(worker.wait(), timeout=5)

        await app._run_trace("/trace status")
        await pilot.pause()

    assert get_tracing_config().is_configured()
