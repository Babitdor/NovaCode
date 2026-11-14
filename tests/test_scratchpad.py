"""Tests for session-scoped scratchpad provisioning, policy, and cleanup."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from novacode_cli.config.plan_mode import active_plan_session
from novacode_cli.security import policy as policy_mod
from novacode_cli.security.policy import ApprovalPolicy, load_policy, reset_policy_cache
from novacode_cli.sessions import scratchpad

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate policy cache/HOME and scratchpad registry between tests."""
    monkeypatch.setattr(policy_mod, "HOME_DIR", tmp_path / "empty_home")
    monkeypatch.setattr(scratchpad.tempfile, "gettempdir", lambda: str(tmp_path))
    token = active_plan_session.set(None)
    reset_policy_cache()
    # Ensure no leaked active scratchpads affect policy checks.
    with scratchpad._lock:
        scratchpad._active.clear()
    yield
    with scratchpad._lock:
        ids = list(scratchpad._active)
    for sid in ids:
        scratchpad.cleanup(sid)
    active_plan_session.reset(token)
    reset_policy_cache()


def _policy() -> ApprovalPolicy:
    return load_policy(project_root=Path(__file__).parent / "_no_such_dir")


def test_provision_creates_unique_dir_per_session():
    a = scratchpad.provision("sess-A")
    b = scratchpad.provision("sess-B")
    assert a is not None
    assert b is not None
    assert a.host_path.is_dir()
    assert b.host_path.is_dir()
    assert a.host_path != b.host_path
    assert a.virtual_prefix == "/scratchpad/"
    assert (
        json.loads((a.host_path / ".nova-scratchpad").read_text(encoding="utf-8"))["session_id"]
        == "sess-A"
    )
    assert "sess-A" in a.host_path.name
    assert "sess-B" in b.host_path.name


def test_provision_is_idempotent_and_registered():
    a = scratchpad.provision("same-session")
    b = scratchpad.provision("same-session")
    assert a is not None
    assert b is not None
    assert a.host_path == b.host_path
    active_plan_session.set(SimpleNamespace(session_id="same-session"))
    assert scratchpad.get("same-session") is not None
    assert scratchpad.is_under_scratchpad(a.host_path / "note.txt")
    assert scratchpad.is_under_scratchpad(a.host_path)


def test_policy_allows_file_ops_inside_scratchpad_and_not_outside(tmp_path: Path):
    info = scratchpad.provision("policy-session")
    assert info is not None
    active_plan_session.set(SimpleNamespace(session_id="policy-session"))
    p = _policy()
    inside = str(info.host_path / "draft.py")
    assert p.evaluate("write_file", {"file_path": inside}).tier == "allow"
    assert p.evaluate("edit_file", {"file_path": inside}).tier == "allow"
    # A normal project path still follows the default tool tier (ask), not scratchpad allow.
    outside = str(tmp_path / "project" / "file.py")
    assert p.evaluate("write_file", {"file_path": outside}).tier == "ask"


def test_cleanup_removes_directory_and_unregisters():
    info = scratchpad.provision("cleanup-session")
    assert info is not None
    # Put a file inside to ensure recursive removal.
    (info.host_path / "temp.txt").write_text("x", encoding="utf-8")
    assert info.host_path.exists()
    scratchpad.cleanup("cleanup-session")
    assert not info.host_path.exists()
    assert scratchpad.get("cleanup-session") is None
    assert not scratchpad.is_under_scratchpad(info.host_path / "temp.txt")


def test_sweep_removes_stale_owned_dirs_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Force tempdir to a controlled location for this test.
    base = tmp_path / "tmpbase"
    base.mkdir()
    monkeypatch.setattr(scratchpad.tempfile, "gettempdir", lambda: str(base))

    stale = base / "nova-scratchpad-stale-one"
    stale.mkdir()
    (stale / ".nova-scratchpad").write_text(json.dumps({"pid": 987654321}), encoding="utf-8")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))

    fresh = base / "nova-scratchpad-fresh-one"
    fresh.mkdir()
    (fresh / ".nova-scratchpad").write_text("fresh-one", encoding="utf-8")

    foreign = base / "nova-scratchpad-foreign"
    foreign.mkdir()
    # No marker -> must not be removed by sweep.
    old2 = time.time() - 48 * 3600
    os.utime(foreign, (old2, old2))

    removed = scratchpad.sweep_stale(max_age_hours=24)
    assert removed >= 1
    assert not stale.exists()
    assert fresh.exists()
    assert foreign.exists()


def test_policy_virtual_mount_session_isolation_and_denies():
    first = scratchpad.provision("first")
    second = scratchpad.provision("second")
    active_plan_session.set(SimpleNamespace(session_id="first"))
    p = _policy()
    assert p.evaluate("write_file", {"file_path": "/scratchpad/draft.py"}).tier == "allow"
    assert (
        p.evaluate("write_file", {"file_path": str(first.host_path / "draft.py")}).tier == "allow"
    )
    assert (
        p.evaluate("write_file", {"file_path": str(second.host_path / "draft.py")}).tier != "allow"
    )
    assert p.evaluate("write_file", {"file_path": "/scratchpad/../escape.py"}).tier != "allow"
    p.path_deny.append("**/draft.py")
    assert p.evaluate("write_file", {"file_path": "/scratchpad/draft.py"}).tier == "deny"
    p.tool_tiers["write_file"] = "deny"
    assert p.evaluate("write_file", {"file_path": "/scratchpad/other.py"}).tier == "deny"


def test_session_ids_that_sanitize_identically_are_isolated():
    assert scratchpad.provision("a/b").host_path != scratchpad.provision("a?b").host_path


def test_clear_removes_previous_conversation_scratchpad():
    from novacode_cli.states.Session import SessionState

    state = SessionState()
    previous = scratchpad.provision(state.session_id)
    state.reset_conversation()
    assert not previous.host_path.exists()
    assert scratchpad.get(state.session_id) is None


def test_symlink_escape_is_not_preapproved(tmp_path: Path):
    info = scratchpad.provision("links")
    active_plan_session.set(SimpleNamespace(session_id="links"))
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (info.host_path / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires OS privileges on this host")
    assert not scratchpad.is_under_scratchpad(info.host_path / "escape" / "draft.py")
    assert (
        _policy().evaluate("write_file", {"file_path": "/scratchpad/escape/draft.py"}).tier
        != "allow"
    )


async def test_worker_shutdown_removes_its_scratchpad(monkeypatch: pytest.MonkeyPatch):
    from tests.test_session_worker import _make

    worker, _ = _make(monkeypatch, [])
    info = scratchpad.provision(worker.session_state.session_id)
    await worker.inbox.put({"t": "shutdown"})
    await worker.run()
    assert not info.host_path.exists()


async def test_headless_failure_removes_its_scratchpad(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.headless.runner import run_headless
    from tests.test_headless import _headless_state

    state = _headless_state()
    info = scratchpad.provision(state.session_id)

    async def failed(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        yield  # Make this an async generator like the real event stream.
        error = "synthetic failure"
        raise RuntimeError(error)

    monkeypatch.setattr("novacode_cli.headless.runner.iterate_agent_events", failed)
    await run_headless(agent=object(), assistant_id="nova", session_state=state)
    assert not info.host_path.exists()


def test_sweep_keeps_another_live_process_directory():
    info = scratchpad.provision("live")
    with scratchpad._lock:
        scratchpad._active.clear()
    old = time.time() - 48 * 3600
    os.utime(info.host_path, (old, old))
    assert scratchpad.sweep_stale() == 0
    assert info.host_path.is_dir()
    scratchpad._active["live"] = info.host_path


def test_scratchpad_mount_and_prompt_rotate_on_clear():
    from unittest.mock import Mock

    from deepagents.backends import FilesystemBackend
    from langchain.agents.middleware.types import ModelRequest

    from novacode_cli.agents.scratchpad import ScratchpadMiddleware

    original = scratchpad.provision("original")
    backend = FilesystemBackend(root_dir=str(original.host_path), virtual_mode=True)
    middleware = ScratchpadMiddleware(backend, "original")
    active_plan_session.set(SimpleNamespace(session_id="new"))
    request = ModelRequest(model=Mock(), messages=[], system_message=None)
    updated = middleware._request(request)
    current = scratchpad.get("new")
    assert backend.cwd == current.host_path
    assert not original.host_path.exists()
    assert str(current.host_path) in updated.system_message.content_blocks[-1]["text"]
    backend.write("/draft.txt", "scratch only")
    assert (current.host_path / "draft.txt").read_text() == "scratch only"
