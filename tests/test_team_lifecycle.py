"""Session reuse and compatibility identities must remain root scoped."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from langchain_core.runnables.config import var_child_runnable_config

from novacode_cli.agents.agent_mailbox import clear_mailbox, read_agent_messages, send_agent_message
from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.host import TeamHost
from novacode_cli.teams.legacy import mailbox_identity
from novacode_cli.teams.middleware import TeamMiddleware
from novacode_cli.teams.models import Identity, TeamError

if TYPE_CHECKING:
    from pathlib import Path


async def test_clear_reuses_graph_without_reusing_closed_root(tmp_path: Path):
    def factory(root: str) -> TeamCoordinator:
        return TeamCoordinator(tmp_path, Identity("project", root), {})

    host = TeamHost(factory("first"), factory, lambda *_: None)
    first = host.bind_session(SimpleNamespace(session_id="first", thread_id="thread1"))
    team = await first.request(first.lead, "team_create", {"name": "one"})
    second = host.bind_session(SimpleNamespace(session_id="second", thread_id="thread2"))
    middleware = TeamMiddleware(host.binding(None), host=host)
    for thread, expected in (("thread1", first), ("thread2", second)):
        token = var_child_runnable_config.set({"configurable": {"thread_id": thread}})
        try:
            assert middleware._binding().coordinator is expected
        finally:
            var_child_runnable_config.reset(token)
    await host.release_session("thread1")
    assert first._closed
    assert not second._closed
    with pytest.raises(TeamError):
        await second.request(second.lead, "task_list", {"team_id": team["id"]})
    await host.release_session("thread2")


def test_legacy_wrappers_bind_sender_and_prevent_cross_root_reads():
    first = mailbox_identity.set(("project", "one", "", "reviewer"))
    try:
        send_agent_message.invoke({"to_agent": "lead", "message": "private", "from_agent": "spoof"})
        assert "cannot read" in read_agent_messages.invoke({"as_agent": "lead"})
        clear_mailbox()  # Legacy /clear cannot erase a scoped conversation.
    finally:
        mailbox_identity.reset(first)
    for root, expected in (("two", "No messages."), ("one", "[from reviewer] private")):
        token = mailbox_identity.set(("project", root, "", "lead"))
        try:
            assert read_agent_messages.invoke({"as_agent": "lead"}) == expected
        finally:
            mailbox_identity.reset(token)


async def test_restart_keeps_uncertain_worker_disconnected_and_never_replays(tmp_path: Path):
    host = TeamCoordinator(tmp_path, Identity("project", "root"), {})
    team = await host.request(host.lead, "team_create", {"name": "one"})
    worker = await host._db("add_agent", host.lead, team["id"], "reviewer", "fork", str(tmp_path))
    await host._db("agent_status", host.lead, worker["id"], "running")
    await host.close()  # Simulate a lost transport without an attached process handle.
    restarted = TeamCoordinator(tmp_path, host.lead, {})
    record = await restarted.request(restarted.lead, "get_agent", {"agent_id": worker["id"]})
    assert record["status"] == "disconnected"
    with pytest.raises(TeamError, match="uncertain"):
        await restarted.cancel(restarted.lead, worker["id"])
    # Test fixture has no real external worker. Supply explicit absence proof.
    restarted._handles[worker["id"]].recovery = {"kind": "absent"}
    await restarted.close()
