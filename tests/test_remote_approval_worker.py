"""A child must execute the same approval snapshot its parent reviewed."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from novacode_cli import ui_events as ev
from novacode_cli.sessions import protocol
from novacode_cli.sessions.worker import SessionWorker


async def test_plan_snapshot_survives_ipc_and_later_saved_plan_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = ["Plan A: inspect and test"]
    resolve = Mock(side_effect=lambda *_args, **_kwargs: (saved[0], None))
    monkeypatch.setattr("novacode_cli.ui.interrupt_handlers.resolve_plan_content", resolve)
    worker = SessionWorker(agent=None, assistant_id="nova", session_state=SimpleNamespace())
    frames = []
    emitted = asyncio.Event()

    def emit(frame: dict) -> None:
        # Exercise the actual serialization boundary rather than sharing dicts.
        frames.append(json.loads(protocol.dumps(frame)))
        emitted.set()

    worker._emit = emit
    original = {"plan": ""}
    event = ev.InterruptRequest("plan", original, asyncio.get_running_loop().create_future())
    task = asyncio.create_task(worker._forward_interrupt(event))
    await asyncio.wait_for(emitted.wait(), 2)
    parent = frames[0]
    assert parent["payload"]["plan"] == saved[0]
    assert parent["payload"] is not original
    saved[0] = "Plan B: never reviewed"
    worker._pending[parent["id"]].set_result(
        {
            "response": {"approved": True, "mode": "manual"},
            "state_update": {"plan_mode_enabled": False},
        }
    )
    await asyncio.wait_for(task, 2)
    assert original["plan"] == "Plan A: inspect and test"
    assert event.future.result()["response"]["approved"]
    resolve.assert_called_once()
    assert not worker._pending


@pytest.mark.parametrize("kind", ["tool", "plan"])
async def test_lossy_approval_payload_cannot_be_authorized(kind: str) -> None:
    worker = SessionWorker(agent=None, assistant_id="nova", session_state=SimpleNamespace())
    worker._emit = Mock()
    text = "x" * (protocol.MAX_FIELD + 1)
    payload = (
        {"plan": text}
        if kind == "plan"
        else {
            "action_requests": [{"name": "write_file", "args": {"content": text}}],
        }
    )
    event = ev.InterruptRequest(kind, payload, asyncio.get_running_loop().create_future())
    await worker._forward_interrupt(event)
    worker._emit.assert_not_called()
    result = event.future.result()
    assert result.get("any_rejected") if kind == "tool" else not result["response"]["approved"]
    assert not worker._pending


async def test_plan_snapshot_resolution_failure_rejects_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "novacode_cli.ui.interrupt_handlers.resolve_plan_content",
        Mock(side_effect=OSError("unreadable plan")),
    )
    worker = SessionWorker(agent=None, assistant_id="nova", session_state=SimpleNamespace())
    worker._emit = Mock()
    event = ev.InterruptRequest("plan", {}, asyncio.get_running_loop().create_future())
    await worker._forward_interrupt(event)
    assert not event.future.result()["response"]["approved"]
    assert not worker._pending
    worker._emit.assert_not_called()


@pytest.mark.parametrize("kind", ["tool", "plan"])
async def test_child_cannot_execute_mutated_details_under_old_parent_approval(kind: str) -> None:
    worker = SessionWorker(agent=None, assistant_id="nova", session_state=SimpleNamespace())
    emitted = asyncio.Event()
    frames = []

    def emit(frame: dict) -> None:
        frames.append(json.loads(protocol.dumps(frame)))
        emitted.set()

    worker._emit = emit
    original = (
        {"plan": "Reviewed plan"}
        if kind == "plan"
        else {
            "action_requests": [{"name": "shell", "args": {"command": "reviewed"}}],
        }
    )
    event = ev.InterruptRequest(kind, original, asyncio.get_running_loop().create_future())
    task = asyncio.create_task(worker._forward_interrupt(event))
    await asyncio.wait_for(emitted.wait(), 2)
    if kind == "plan":
        original["plan"] = "Different plan"
        result = {"response": {"approved": True, "mode": "manual"}}
    else:
        original["action_requests"][0]["args"]["command"] = "different command"
        result = {"decisions": [{"type": "approve"}], "any_rejected": False}
    worker._pending[frames[0]["id"]].set_result(result)
    await asyncio.wait_for(task, 2)
    rejected = event.future.result()
    assert rejected.get("any_rejected") if kind == "tool" else not rejected["response"]["approved"]
    assert not worker._pending
