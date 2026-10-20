"""The approved identity, claim, fencing, restart and bounded-inbox contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from novacode_cli.teams.models import Identity, Limits, TeamError
from novacode_cli.teams.store import TeamStore

if TYPE_CHECKING:
    from pathlib import Path


def setup_team(tmp_path: Path):
    store = TeamStore(tmp_path / "teams.sqlite3")
    lead = Identity("project", "root")
    team = store.create_team(lead, "implementation")
    first = store.add_agent(lead, team["id"], "reviewer", "default", "/workspace")
    second = store.add_agent(lead, team["id"], "reviewer", "default", "/workspace")
    return (
        store,
        lead,
        team,
        Identity("project", "root", first["id"], team["id"]),
        Identity("project", "root", second["id"], team["id"]),
    )


def test_foreign_root_and_forged_members_cannot_read_or_mutate(tmp_path: Path):
    store, lead, team, member, _ = setup_team(tmp_path)
    task = store.create_task(lead, team["id"], "review", "check invariants")
    for stranger in (
        Identity("project", "other"),
        Identity("other", "root"),
        Identity("project", "root", "forged", team["id"]),
    ):
        with pytest.raises(TeamError):
            store.get_task(stranger, task["id"])
        with pytest.raises(TeamError):
            store.send(stranger, team["id"], member.agent, "secret", "m1")
    with pytest.raises(TeamError):
        store.create_task(member, team["id"], "impersonation", "no")
    store.close()


def test_concurrent_claim_has_exactly_one_winner_across_connections(tmp_path: Path):
    store, lead, team, first, second = setup_team(tmp_path)
    task = store.create_task(lead, team["id"], "review", "check invariants")

    def claim(identity: Identity) -> dict | None:
        connection = TeamStore(tmp_path / "teams.sqlite3")
        try:
            return connection.claim(identity, task["id"], 0)
        except TeamError:
            return None
        finally:
            connection.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, (first, second)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.get_task(lead, task["id"])["owner"] == winners[0]["owner"]
    store.close()


def test_dependencies_expiry_and_stale_completion_fail_closed(tmp_path: Path):
    store, lead, team, member, _ = setup_team(tmp_path)
    prerequisite = store.create_task(lead, team["id"], "first", "read")
    task = store.create_task(lead, team["id"], "second", "verify", [prerequisite["id"]])
    with pytest.raises(TeamError, match="depend"):
        store.claim(member, task["id"], 0)
    claimed = store.claim(member, prerequisite["id"], 0, now=10)
    with pytest.raises(TeamError, match="lease"):
        store.update_task(
            member,
            prerequisite["id"],
            claimed["version"],
            "completed",
            {"verification": "test passed"},
            now=1000,
        )
    assert store.get_task(lead, prerequisite["id"])["status"] == "running"
    with pytest.raises(TeamError):
        store.claim(member, prerequisite["id"], 1, now=1000)
    store.close()


def test_message_idempotency_bound_identity_ack_and_restart(tmp_path: Path):
    store, lead, team, first, second = setup_team(tmp_path)
    sent = store.send(first, team["id"], second.agent, "hello", "request-1")
    assert store.send(first, team["id"], second.agent, "hello", "request-1")["id"] == sent["id"]
    with pytest.raises(TeamError, match="request"):
        store.send(first, team["id"], second.agent, "changed", "request-1")
    assert store.inbox(first, team["id"]) == []
    with pytest.raises(TeamError):
        store.ack(first, sent["id"])
    store.close()
    reopened = TeamStore(tmp_path / "teams.sqlite3")
    assert reopened.inbox(second, team["id"])[0]["sender"] == first.agent
    reopened.ack(second, sent["id"])
    assert reopened.inbox(second, team["id"]) == []
    assert reopened.get_team(lead, team["id"])["status"] == "active"
    reopened.close()


def test_backpressure_does_not_drop_unread_messages(tmp_path: Path):
    store, _lead, team, first, second = setup_team(tmp_path)
    store.limits = Limits(pending_messages=1)
    store.send(first, team["id"], second.agent, "first", "a")
    with pytest.raises(TeamError, match="limit"):
        store.send(first, team["id"], second.agent, "second", "b")
    assert [message["text"] for message in store.inbox(second, team["id"])] == ["first"]
    with pytest.raises(TeamError):
        store.send(first, team["id"], second.agent, "x" * 20000, "c")
    store.close()
