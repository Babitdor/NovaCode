"""The /trace setup modal collects the four LangSmith environment variables."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Button, Input, Select, Static

from novacode_cli.tui.screens import LangSmithSetupModal


class Host(App):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.result = "not-dismissed"

    def on_mount(self):
        self.push_screen(self.panel, self._store)

    def _store(self, value):
        self.result = value


@pytest.mark.asyncio
async def test_save_returns_the_entered_environment():
    modal = LangSmithSetupModal(
        current={
            "LANGSMITH_TRACING": "true",
            "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            "LANGSMITH_API_KEY": "lsv2_old",
            "LANGSMITH_PROJECT": "Nova-Code",
            "LANGSMITH_WORKSPACE_ID": "workspace-1",
            "LANGSMITH_AGENT_ID": "novacode-cli",
            "LANGSMITH_AGENT_ENVIRONMENT": "production",
        }
    )
    host = Host(modal)
    async with host.run_test(size=(100, 40)) as pilot:
        modal.query_one("#langsmith-endpoint", Input).value = "https://example.test"
        modal.query_one("#langsmith-api-key", Input).value = "lsv2_new"
        modal.query_one("#langsmith-project", Input).value = "MyProject"
        modal.query_one("#langsmith-tracing", Select).value = "true"
        await pilot.pause()
        modal.query_one("#do-save", Button).press()
        await pilot.pause()

    assert host.result == {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_ENDPOINT": "https://example.test",
        "LANGSMITH_API_KEY": "lsv2_new",
        "LANGSMITH_PROJECT": "MyProject",
        "LANGSMITH_WORKSPACE_ID": "workspace-1",
        "LANGSMITH_AGENT_ID": "novacode-cli",
        "LANGSMITH_AGENT_ENVIRONMENT": "production",
    }


@pytest.mark.asyncio
async def test_form_is_prefilled_with_the_current_environment():
    modal = LangSmithSetupModal(
        current={
            "LANGSMITH_TRACING": "false",
            "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            "LANGSMITH_API_KEY": "lsv2_current",
            "LANGSMITH_PROJECT": "CurrentProject",
        }
    )
    host = Host(modal)
    async with host.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert str(modal.query_one("#langsmith-tracing", Select).value) == "false"
        assert modal.query_one("#langsmith-endpoint", Input).value == "https://api.smith.langchain.com"
        assert modal.query_one("#langsmith-api-key", Input).value == "lsv2_current"
        assert modal.query_one("#langsmith-project", Input).value == "CurrentProject"


@pytest.mark.asyncio
async def test_tracing_without_an_api_key_is_refused():
    modal = LangSmithSetupModal(current={"LANGSMITH_TRACING": "true"})
    host = Host(modal)
    async with host.run_test(size=(100, 40)) as pilot:
        modal.query_one("#langsmith-tracing", Select).value = "true"
        modal.query_one("#langsmith-api-key", Input).value = ""
        await pilot.pause()
        modal.query_one("#do-save", Button).press()
        await pilot.pause()

        assert host.result == "not-dismissed", "the modal closed without an API key"
        hint = str(modal.query_one("#langsmith-hint", Static).content)
        assert "API key" in hint


@pytest.mark.asyncio
async def test_cancel_dismisses_without_values():
    modal = LangSmithSetupModal(current={})
    host = Host(modal)
    async with host.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        modal.query_one("#cancel", Button).press()
        await pilot.pause()

    assert host.result is None


async def test_setup_save_is_reachable_in_small_terminal():
    modal = LangSmithSetupModal(current={"LANGSMITH_API_KEY": "fake"})
    host = Host(modal)
    async with host.run_test(size=(80, 24)) as pilot:
        modal.query_one("#do-save", Button).focus()
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        await pilot.click("#do-save")
        await pilot.pause()
    assert host.result["LANGSMITH_API_KEY"] == "fake"
