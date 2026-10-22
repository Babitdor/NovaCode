"""Approval replies are bound to exact calls and current effective policy."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.states.Session import Notification, SessionState
from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.models import Identity

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def fixed_policy(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.security import policy

    known = policy.ApprovalPolicy(
        tool_tiers={"read_file": "allow", "shell": "deny", "write_file": "ask"},
        shell_allow=[],
        shell_deny=[],
        path_allow=[],
        path_deny=[],
        domain_allow=[],
        domain_deny=[],
    )
    monkeypatch.setattr(policy, "get_policy", lambda *_args, **_kwargs: known)


async def setup_worker(tmp_path: Path):
    adapter = AsyncMock()
    adapter.start.return_value.stop.return_value = True
    adapter.start.return_value.request.cwd = tmp_path
    host = TeamCoordinator(tmp_path, Identity("p", "r"), {"default": adapter}, workspace=tmp_path)
    team = await host.request(host.lead, "team_create", {"name": "test"})
    member = await host.spawn(host.lead, "read", team["id"])
    return host, Identity("p", "r", member["id"], team["id"])


async def test_readonly_allowed_but_explicit_deny_survives_auto_approve(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    host.session_state = SessionState(auto_approve=True)
    allowed = await host.authorize(
        member, {"tool": "read_file", "args": {"file_path": "/a"}, "tool_call_id": "read"}
    )
    denied = await host.authorize(
        member, {"tool": "shell", "args": {"command": "rm -rf /"}, "tool_call_id": "deny"}
    )
    assert allowed["allowed"]
    assert not denied["allowed"]
    await host.close()


async def test_user_decision_is_required_and_waiters_are_released(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    state = host.session_state = SessionState()

    def approve(notification: Notification) -> None:
        if notification.action_id:
            state.resolve_approval(notification.action_id, approve=True)

    state._notification_callback = approve
    result = await host.authorize(
        member,
        {
            "tool": "write_file",
            "args": {"file_path": "/a", "content": "approved"},
            "tool_call_id": "w1",
        },
    )
    assert result["allowed"]
    assert not state._pending_approvals
    await host.close()


async def test_cancelled_approval_never_executes_or_leaks_future(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    state = host.session_state = SessionState()
    pending = asyncio.Event()
    state._notification_callback = lambda _: pending.set()
    operation = asyncio.create_task(
        host.authorize(
            member,
            {
                "tool": "write_file",
                "args": {"file_path": "/a"},
                "tool_call_id": "w2",
            },
        )
    )
    await asyncio.wait_for(pending.wait(), timeout=5)
    assert state._pending_approvals
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not state._pending_approvals
    await host.close()


async def test_explicit_read_ask_is_forwarded_instead_of_implicitly_allowed(tmp_path: Path):
    from novacode_cli.security.policy import get_policy

    host, member = await setup_worker(tmp_path)
    get_policy().tool_tiers["read_file"] = "ask"
    result = await host.authorize(
        member, {"tool": "read_file", "args": {"file_path": "/a"}, "tool_call_id": "r"}
    )
    assert not result["allowed"]  # No parent UI means ask must fail closed.
    await host.close()


async def test_policy_change_invalidates_pending_approval(tmp_path: Path):
    from novacode_cli.security.policy import get_policy

    host, member = await setup_worker(tmp_path)
    state = host.session_state = SessionState()
    pending = asyncio.Event()
    state._notification_callback = lambda _: pending.set()
    request = asyncio.create_task(
        host.authorize(
            member,
            {
                "tool": "write_file",
                "args": {"file_path": "/a", "content": "yes"},
                "tool_call_id": "w",
            },
        )
    )
    await pending.wait()
    get_policy().tool_tiers["write_file"] = "deny"
    for action in list(state._pending_approvals):
        state.resolve_approval(action, approve=True)
    assert not (await request)["allowed"]
    assert not state._pending_approvals
    await host.close()


async def test_stopping_agent_settles_its_pending_approval(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    state = host.session_state = SessionState()
    pending = asyncio.Event()
    state._notification_callback = lambda _: pending.set()
    request = asyncio.create_task(
        host.authorize(
            member,
            {
                "tool": "write_file",
                "args": {"file_path": "/a", "content": "yes"},
                "tool_call_id": "w",
            },
        )
    )
    await pending.wait()
    await host.cancel(host.lead, member.agent)
    result = await asyncio.wait_for(request, timeout=1)
    assert not result["allowed"]
    assert not state._pending_approvals
    await host.close()


async def test_granted_call_id_cannot_authorize_a_second_execution(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    call = {"tool": "read_file", "args": {"file_path": "/a"}, "tool_call_id": "once"}
    assert (await host.authorize(member, call))["allowed"]
    assert not (await host.authorize(member, call))["allowed"]
    await host.close()


async def test_external_write_requires_real_approval_even_in_auto_mode(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    host.session_state = SessionState(auto_approve=True)
    external = tmp_path.parent / "Research" / "report.md"
    result = await host.authorize(
        member,
        {
            "tool": "write_file",
            "args": {"file_path": str(external)},
            "tool_call_id": "outside",
            "write_paths": [str(external)],
        },
    )
    assert not result["allowed"]
    assert "outside the project" in result["reason"]
    await host.close()


async def test_explicit_external_call_approval_is_preserved(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    state = host.session_state = SessionState(auto_approve=True)
    external = tmp_path.parent / "Research" / "report.md"
    notices = []

    def approve(notification: Notification) -> None:
        notices.append(notification)
        state.resolve_approval(notification.action_id, approve=True)

    state._notification_callback = approve
    call = {
        "tool": "write_file",
        "args": {"file_path": str(external)},
        "tool_call_id": "approved-external",
        "write_paths": [str(external)],
    }
    assert (await host.authorize(member, call))["allowed"]
    assert len(notices) == 1
    assert not (await host.authorize(member, call))["allowed"]
    await host.close()


async def test_explicit_path_allow_grants_external_destination(tmp_path: Path):
    from novacode_cli.security.policy import get_policy

    host, member = await setup_worker(tmp_path)
    host.session_state = SessionState(auto_approve=True)
    external = tmp_path.parent / "Research" / "report.md"
    get_policy().path_allow.append(external.as_posix())
    assert (
        await host.authorize(
            member,
            {
                "tool": "write_file",
                "args": {"file_path": str(external)},
                "tool_call_id": "allowed-root",
                "write_paths": [str(external)],
            },
        )
    )["allowed"]
    get_policy().path_deny.append(external.as_posix())
    assert not (
        await host.authorize(
            member,
            {
                "tool": "write_file",
                "args": {"file_path": str(external)},
                "tool_call_id": "denied-root",
                "write_paths": [str(external)],
            },
        )
    )["allowed"]
    await host.close()


async def test_in_project_write_stays_automatic(tmp_path: Path):
    host, member = await setup_worker(tmp_path)
    host.session_state = SessionState(auto_approve=True)
    root = host.workspace
    assert (
        await host.authorize(
            member,
            {
                "tool": "write_file",
                "args": {"file_path": "/notes/report.md"},
                "tool_call_id": "inside",
                "write_paths": [str(root / "notes/report.md")],
            },
        )
    )["allowed"]
    await host.close()
