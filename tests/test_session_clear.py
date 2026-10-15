"""Tests for /clear excluding sessions from both auto-resume and the picker.

Regression: after /clear, `nova --resume` (the interactive picker) still listed
and could restore the cleared conversation because the picker didn't filter on
the `cleared` flag (unlike --continue's get_latest_session).
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.session.session_restore import select_session_interactive


def _manager_with_sessions() -> SessionManager:
    sm = SessionManager(sessions_dir=pathlib.Path(tempfile.mkdtemp()))
    msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
    sm.save_session(session_id="keep", thread_id="t1", messages=msgs, assistant_id="nova-agent")
    sm.save_session(session_id="gone", thread_id="t2", messages=msgs, assistant_id="nova-agent")
    sm.mark_cleared("gone")
    return sm


def test_get_latest_session_skips_cleared():
    sm = _manager_with_sessions()
    latest = sm.get_latest_session()
    assert latest is not None
    assert latest.session_id == "keep"


def test_explicit_load_of_cleared_still_works_for_recovery():
    # Cleared sessions remain on disk and recoverable by explicit id.
    sm = _manager_with_sessions()
    data = sm.load_session("gone")
    assert data is not None
    assert data.meta.cleared is True


async def test_resume_picker_excludes_cleared(monkeypatch):
    sm = _manager_with_sessions()

    captured: dict = {}

    async def fake_pick(sessions):
        captured["ids"] = [s.session_id for s in sessions]
        return sessions[0].session_id if sessions else None

    # Force the native picker path and capture what it's handed.
    monkeypatch.setattr(
        "novacode_cli.tui.pickers.pick_session_tui", fake_pick, raising=True
    )
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    selected = await select_session_interactive(sm)
    assert "gone" not in captured["ids"]
    assert captured["ids"] == ["keep"]
    assert selected == "keep"


async def test_resume_picker_lists_only_this_project(monkeypatch):
    """`nova --resume` lists the sessions of the project it was started in."""
    sm = SessionManager(sessions_dir=pathlib.Path(tempfile.mkdtemp()))
    msgs = [HumanMessage(content="hi")]
    here, there = pathlib.Path(tempfile.mkdtemp()), pathlib.Path(tempfile.mkdtemp())
    sm.save_session(session_id="mine", thread_id="t1", messages=msgs, assistant_id="nova", project_root=here)
    sm.save_session(session_id="other", thread_id="t2", messages=msgs, assistant_id="nova", project_root=there)

    captured: dict = {}

    async def fake_pick(sessions):
        captured["ids"] = [s.session_id for s in sessions]
        return None

    monkeypatch.setattr("novacode_cli.tui.pickers.pick_session_tui", fake_pick, raising=True)
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    await select_session_interactive(sm, project_root=here)
    assert captured["ids"] == ["mine"]
