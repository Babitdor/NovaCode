"""Autosaves capture prompts, checkpoints and the owning session across tab switches."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from novacode_cli.session.session_persistence import SessionManager
from tests.test_tui_subagent_panel import _app


@pytest.mark.asyncio
async def test_prompt_saved_before_stream_and_completed_state_saved(tmp_path: Path):
    app = _app()
    app.session_state.session_id = "recover"
    app.session_manager = SessionManager(tmp_path)
    history = []

    async def state(_config: dict) -> SimpleNamespace:
        return SimpleNamespace(values={"messages": history})

    async def stream(text: str, _assistant_id: str | None) -> None:
        assert app.session_manager.load_session("recover").messages[-1].content == text
        history.append(HumanMessage(text))
        message = "unexpected model failure"
        raise RuntimeError(message)

    app.agent.aget_state = state
    app._do_stream = stream
    app._set_status = lambda _text: None
    app._log = lambda _text: None
    app._stop_foreground_subagents = lambda: None
    app._clear_live_steers = lambda: None
    app._set_nova_indicator = lambda _text: None

    async def no_refresh() -> None:
        pass

    app._update_context_breakdown = no_refresh
    app._check_context = no_refresh
    app._drain_deferred_commands = no_refresh
    app._drain_deferred_prompts = no_refresh
    await app._stream_prompt("save this request")
    restored = app.session_manager.load_session("recover")
    assert [message.content for message in restored.messages] == ["save this request"]


@pytest.mark.asyncio
async def test_autosave_captures_owner_even_if_tab_changes(tmp_path: Path):
    app = _app()
    app.session_state.session_id = "root"
    original_state = app.session_state
    app.session_manager = SessionManager(tmp_path)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def state(_config: dict) -> SimpleNamespace:
        entered.set()
        await finish.wait()
        return SimpleNamespace(values={"messages": [HumanMessage("root history")]})

    app.agent.aget_state = state
    saving = asyncio.create_task(app._save_session())
    await entered.wait()
    app.session_state = SimpleNamespace(thread_id="child-thread", session_id="child")
    app.model_name = "child-model"
    finish.set()
    await saving
    restored = app.session_manager.load_session("root")
    assert restored.meta.thread_id == original_state.thread_id
    assert restored.meta.model_name == "deepseek-v4.1-flash"
    assert app.session_manager.load_session("child") is None


@pytest.mark.asyncio
async def test_pending_prompt_survives_unavailable_graph(tmp_path: Path):
    app = _app()
    app.session_state.session_id = "root"
    app.session_manager = SessionManager(tmp_path)
    app.session_manager.save_session("root", "t1", [HumanMessage("earlier")], "nova")

    async def unavailable(_config: dict) -> SimpleNamespace:
        message = "checkpoint unavailable"
        raise RuntimeError(message)

    app.agent.aget_state = unavailable
    await app._save_session(pending_prompt="incoming", task_status="interrupted")
    await app._save_session()
    assert [m.content for m in app.session_manager.load_session("root").messages] == [
        "earlier",
        "incoming",
    ]
