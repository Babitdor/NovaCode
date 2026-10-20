"""The automatic dream's three gates (time, sessions, lock) and /dream's verbs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.memory import dream_gates as gates

if TYPE_CHECKING:
    from pathlib import Path


def _session(sessions: Path, name: str, started: datetime, messages: int = 3) -> None:
    folder = sessions / name
    folder.mkdir(parents=True)
    meta = {"created_at": started.isoformat(), "message_count": messages}
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _set_last_dream(agent_dir: Path, when: datetime) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ".dream.json").write_text(
        json.dumps({"last_dream_at": when.isoformat()}), encoding="utf-8"
    )


def test_first_check_sets_a_baseline_instead_of_dreaming(tmp_path: Path):
    sessions = tmp_path / "sessions"
    old = datetime.now(UTC) - timedelta(days=30)
    for i in range(10):
        _session(sessions, f"s{i}", old)
    report = gates.check_gates(tmp_path / "agent", sessions)
    assert not report.time_ok
    assert report.sessions == 0
    assert not report.open
    assert json.loads((tmp_path / "agent" / ".dream.json").read_text())["trigger"] == "baseline"


def test_all_three_gates_must_pass(tmp_path: Path):
    agent, sessions = tmp_path / "agent", tmp_path / "sessions"
    now = datetime.now(UTC)
    _set_last_dream(agent, now - timedelta(hours=25))
    for i in range(4):
        _session(sessions, f"new{i}", now - timedelta(hours=1))
    _session(sessions, "empty", now - timedelta(hours=1), messages=0)  # never counts
    _session(sessions, "before", now - timedelta(hours=30))  # before the last dream
    report = gates.check_gates(agent, sessions)
    assert report.time_ok
    assert report.sessions == 4
    assert not report.open, "four sessions must not be enough"

    _session(sessions, "fifth", now - timedelta(minutes=5))
    assert gates.check_gates(agent, sessions).open

    lock = gates.DreamLock(agent)
    assert lock.acquire()
    held = gates.check_gates(agent, sessions)
    assert not held.lock_ok
    assert not held.open
    lock.release()
    assert gates.check_gates(agent, sessions).open

    _set_last_dream(agent, now - timedelta(hours=2))
    assert not gates.check_gates(agent, sessions).time_ok


def test_lock_is_exclusive_and_a_stale_one_is_taken_over(tmp_path: Path):
    first, second = gates.DreamLock(tmp_path), gates.DreamLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()

    stale = datetime.now(UTC) - gates.STALE_LOCK - timedelta(minutes=1)
    (tmp_path / ".dream.lock").write_text(json.dumps({"pid": 1, "at": stale.isoformat()}))
    assert first.acquire(), "a lock from a dream that died must not block forever"
    first.release()
    assert not (tmp_path / ".dream.lock").exists()


def test_a_finished_dream_resets_the_time_and_session_gates(tmp_path: Path):
    agent, sessions = tmp_path / "agent", tmp_path / "sessions"
    now = datetime.now(UTC)
    _set_last_dream(agent, now - timedelta(days=3))
    for i in range(6):
        _session(sessions, f"s{i}", now - timedelta(hours=1))
    assert gates.check_gates(agent, sessions).open
    gates.record_dream(agent, "auto", "completed")
    report = gates.check_gates(agent, sessions)
    assert not report.time_ok
    assert report.sessions == 0


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from novacode_cli.config import config
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", AsyncMock())
    agent, sessions = tmp_path / "agent", tmp_path / "sessions"
    monkeypatch.setattr(NovaApp, "_dream_dirs", lambda self: (agent, sessions))
    return agent, sessions


@pytest.mark.asyncio
async def test_status_lists_each_gate_and_auto_toggles(isolated):
    from novacode_cli.config.nova_config import NovaConfig
    from tests.test_tui_sessions import _app

    app = _app()
    logged: list[str] = []
    app._log = lambda renderable, **_: logged.append(str(renderable))
    await app._run_dream("/dream status")
    text = "\n".join(logged)
    assert "time" in text
    assert "sessions  0 since then (needs 5)" in text
    assert "lock      free" in text
    await app._run_dream("/dream auto off")
    assert NovaConfig().get("auto_dream") is False
    await app._run_dream("/dream auto maybe")
    assert "Usage: /dream auto on|off" in logged[-1]


@pytest.mark.asyncio
async def test_manual_dream_waits_for_the_lock(isolated):
    from tests.test_tui_sessions import _app

    agent, _ = isolated
    holder = gates.DreamLock(agent)
    assert holder.acquire()
    app = _app()
    logged: list[str] = []
    app._log = lambda renderable, **_: logged.append(str(renderable))
    app._run_dream_now = AsyncMock()
    await app._run_dream("/dream")
    assert "already running" in logged[-1]
    app._run_dream_now.assert_not_called()
    holder.release()
    await app._run_dream("/dream")
    app._run_dream_now.assert_awaited_once()
    assert gates.DreamLock(agent).acquire(), "the manual dream left its lock behind"


@pytest.mark.asyncio
async def test_auto_dream_runs_only_when_the_gates_open(isolated, monkeypatch):
    from novacode_cli.agents import dream_agent
    from novacode_cli.commands import dream_handler
    from novacode_cli.config import model_create
    from novacode_cli.ui_events import AssistantMessage, Done
    from tests.test_tui_sessions import _app

    agent_dir, sessions = isolated
    runs: list[str] = []

    async def fake_stream(prompt, *args, **kwargs):
        runs.append(prompt)
        yield AssistantMessage(text="Merged two duplicate topics.", agent_name="d", agent_color="")
        yield Done()

    monkeypatch.setattr(dream_handler, "handle_dream_command", AsyncMock(return_value="DREAM"))
    monkeypatch.setattr(model_create, "create_model", lambda: object())
    monkeypatch.setattr(dream_agent, "create_dream_agent", lambda model, path: object())
    monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", fake_stream)

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await app._auto_dream()  # first ever check: baseline, no dream
        assert runs == []

        now = datetime.now(UTC)
        _set_last_dream(agent_dir, now - timedelta(hours=25))
        for i in range(5):
            _session(sessions, f"s{i}", now - timedelta(hours=1))
        await app._auto_dream()
        await pilot.pause()
        assert len(runs) == 1
        assert runs[0].startswith("DREAM")
        assert "Running in the background" in runs[0]
        lines = [str(w.render()) for w in app.query(".logline")]
        assert any("Dreamed: Merged two duplicate topics." in line for line in lines)
        state = json.loads((agent_dir / ".dream.json").read_text())
        assert state["trigger"] == "auto"
        assert gates.DreamLock(agent_dir).acquire(), "the lock was not released"
