"""Proactive agents persist definitions and execute explicit, read-only scopes."""

import asyncio
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from textual.app import App
from textual.widgets import Button, Input, Static, TextArea

from novacode_cli import proactive
from novacode_cli.tui.proactive import ProactiveRuntime, ProactiveScreen
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


def definition(path, **options):
    return proactive.AgentSpec(name="Release Steward", instructions="Report the release status.",
                               workspace=str(path), **options)


async def test_manual_run_persists_and_is_claimed_once(tmp_path):
    path = tmp_path / "agents.sqlite"
    store = proactive.ProactiveStore(path)
    key = store.save(None, definition(tmp_path))
    store.queue(key, "v1.2.36")
    reopened = proactive.ProactiveStore(path)
    assert reopened.agents() == store.agents()
    claims = await asyncio.gather(
        asyncio.to_thread(store.claim, time.time()),
        asyncio.to_thread(reopened.claim, time.time()),
    )
    job, = [claim for claim in claims if claim is not None]
    assert job["context"] == "v1.2.36"
    with pytest.raises(ValueError, match="already"):
        reopened.queue(key, "another")
    result = await proactive.execute_run(job, FakeMessagesListChatModel(
        responses=[AIMessage(content="Release status confirmed.")]
    ))
    reopened.finish(job, "complete", result)
    assert store.history(key)[0]["result"] == "Release status confirmed."


def test_schedule_and_tag_triggers_coalesce_and_can_pause(tmp_path):
    store = proactive.ProactiveStore(tmp_path / "agents.sqlite")
    spec = definition(tmp_path, cron="* * * * *", enabled=True, github="owner/repo", watch_tags=True)
    key = store.save(None, spec)
    now = time.time()
    watch = store.tag_watch(now)
    assert watch[0] == key and watch[2] == "owner/repo"
    assert store.tag_watch(now) is None
    store.tag_failure(key, watch[1])
    assert "Verify gh authentication" in store.trigger_state(key)["tag_error"]
    store.record_tags(key, watch[1], ["v1"])
    assert store.trigger_state(key)["tag_ready"]
    assert not store.trigger_state(key)["tag_error"]
    assert not store.history(key)
    store.record_tags(key, watch[1], ["v2", "v1"])
    job = store.claim(now)
    assert (job["trigger"], job["context"]) == ("GitHub tags", "v2")
    store.finish(job, "complete", "Found the release.")
    store.record_tags(key, watch[1], ["v2", "v1"])
    assert store.claim(now) is None
    due = now + 300
    job = store.claim(due)
    assert job["trigger"] == "Schedule"
    store.save(key, spec.model_copy(update={"enabled": False}))
    store.finish(job, "complete", "Scheduled report")
    assert store.claim(due + 600) is None
    assert store.tag_watch(due + 600) is None
    store.record_tags(key, watch[1], ["v3", "v2"])
    assert len(store.history(key)) == 2


@pytest.mark.parametrize("kwargs", [
    {"github": "owner/repo/extra"}, {"pypi": "https://example.com"},
    {"watch_tags": True}, {"cron": "invalid"},
])
def test_invalid_scopes_and_triggers_fail_validation(tmp_path, kwargs):
    with pytest.raises(ValueError):
        definition(tmp_path, **kwargs)


async def test_git_tools_can_only_read_committed_selected_workspace(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    (tmp_path / "tracked.txt").write_text("Committed evidence")
    git("add", "tracked.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "base")
    (tmp_path / "tracked.txt").write_text("Unsaved content")
    (tmp_path / "private.txt").write_text("Untracked content")
    assert not proactive.tools_for(definition(tmp_path))
    tools = {tool.name: tool for tool in proactive.tools_for(definition(tmp_path, read_workspace=True))}
    assert set(tools) == {"list_workspace_files", "read_workspace_file"}
    read = tools["read_workspace_file"]
    assert await read.ainvoke({"path": "tracked.txt"}) == "Committed evidence"
    assert "Untracked content" not in await read.ainvoke({"path": "private.txt"})
    assert "traversal" in await read.ainvoke({"path": "../private.txt"})
    child = tmp_path / "child"
    child.mkdir()
    listing, _ = proactive.tools_for(definition(child, read_workspace=True))
    assert "repository root" in await listing.ainvoke({})


async def test_oversized_tool_output_stops_a_still_running_process():
    async with asyncio.timeout(8):
        with pytest.raises(ValueError, match="output exceeds"):
            await proactive._read_command(
                sys.executable, "-c",
                "import sys,time; sys.stdout.buffer.write(b'x'*500000); sys.stdout.flush(); time.sleep(30)",
                limit=1024,
            )


@pytest.mark.timeout(30)
async def test_screen_create_run_and_view_history(tmp_path):
    store = proactive.ProactiveStore(tmp_path / "agents.sqlite")
    notices = []
    runtime = ProactiveRuntime(
        store,
        lambda: FakeMessagesListChatModel(responses=[AIMessage(content="Synthetic release report")]),
        lambda message, **_kwargs: notices.append(message),
    )
    app = App()
    try:
        async with app.run_test(size=(48, 28)) as pilot:
            screen = ProactiveScreen(store, str(tmp_path))
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#proactive-name", Input).value = "Release Steward"
            screen.query_one("#proactive-instructions", TextArea).load_text("Check the release.")
            screen.query_one("#proactive-save", Button).press()
            run_button = screen.query_one("#proactive-run", Button)
            async with asyncio.timeout(5):
                while not screen.key or run_button.disabled:
                    await pilot.pause(0.05)
            assert screen.key in dict(store.agents())
            screen.query_one("#proactive-context", Input).value = "v1.2.36"
            run_button.press()
            async with asyncio.timeout(10):
                while not notices:
                    await pilot.pause(0.1)
            await screen.reload_history()
            assert "Synthetic release report" in str(screen.query_one("#proactive-report", Static).content)
            assert notices == ["Release Steward · complete. Open /proactive to see the report."]
    finally:
        await runtime.close()


@pytest.mark.timeout(30)
async def test_slash_command_restores_saved_agents(tmp_path, monkeypatch):
    from novacode_cli.config.config import settings
    from novacode_cli.tui.app import NovaApp

    async def minimal_mount(self):
        self._apply_saved_theme()
        self._ui_harness = SimpleNamespace(applied=SimpleNamespace(commands={}))
        from novacode_cli.tui.proactive import start_proactive

        await start_proactive(self)

    monkeypatch.setattr(NovaApp, "on_mount", minimal_mount)
    store = proactive.ProactiveStore(settings.user_deepagents_dir / "proactive.sqlite3")
    key = store.save(None, definition(tmp_path))
    app = _app()
    async with app.run_test(size=(100, 35)) as pilot:
        assert app._proactive_runtime.store.agents() == store.agents()
        await app._run_slash("/proactive")
        await pilot.pause()
        assert isinstance(app.screen, ProactiveScreen)
        assert app.screen.definitions[key].name == "Release Steward"
    assert all(task.done() for task in app._proactive_runtime.tasks)
