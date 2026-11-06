"""Update restarts may transfer a root lease from their own prior process."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import psutil
import pytest

from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.models import Identity, TeamError
from novacode_cli.teams.store import TeamStore

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Never

    from novacode_cli.teams.store import Record


def test_handoff_reclaims_only_the_named_updater_owner(tmp_path: Path):
    path = tmp_path / "teams.sqlite3"
    identity = Identity("project", "root")
    previous = TeamStore(path)
    previous.acquire_owner(identity, "previous-token")
    # Simulate a safe shutdown retaining the lease for uncertain workers.
    previous._owner = None
    previous.close()

    resumed = TeamStore(path)
    resumed.acquire_owner(identity, "resumed-token", handoff_pid=os.getpid())
    duplicate = TeamStore(path)
    try:
        with pytest.raises(TeamError, match="live coordinator owner"):
            duplicate.acquire_owner(identity, "duplicate-token")
    finally:
        duplicate.close()
        resumed.close()


async def test_handoff_reconciles_old_inprocess_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.teams import recovery

    identity = Identity("project", "root")
    resumed = TeamCoordinator(tmp_path, identity, {}, handoff_pid=os.getpid())
    previous = TeamStore(resumed.path)
    previous.acquire_owner(identity, "previous-token")
    team = previous.create_team(identity, "preserved")
    member = previous.add_agent(identity, team["id"], "old-task", "default", str(tmp_path))
    previous.agent_status(
        identity,
        member["id"],
        "running",
        recovery={"kind": "inprocess", "pid": os.getpid(), "born": psutil.Process().create_time()},
    )
    previous.db.close()  # Retain the lease, as an interrupted update shutdown can.

    unexpected_stop = Mock(
        side_effect=AssertionError("Retired in-process tasks must never signal the updater")
    )
    monkeypatch.setattr(recovery, "stop_process", unexpected_stop)
    try:
        await resumed._db("teams", identity)
        record = await resumed._db("agent", identity, member["id"])
        assert record["status"] == "disconnected"
        with pytest.raises(TeamError, match="no task was replayed"):
            await resumed._handles[member["id"]].send("continue")
        await resumed.cancel(identity, member["id"])
        record = await resumed._db("agent", identity, member["id"])
        assert record["status"] == "stopped"
    finally:
        # Also clean up when exercising the pre-fix failure.
        resumed._handles.clear()
        await resumed.close()


def test_handoff_only_retires_matching_inprocess_records(tmp_path: Path):
    identity = Identity("project", "root")
    path = tmp_path / "teams.sqlite3"
    previous = TeamStore(path)
    previous.acquire_owner(identity, "previous-token")
    team = previous.create_team(identity, "preserved")
    pid, born = os.getpid(), psutil.Process().create_time()
    records = [
        {"kind": "inprocess", "pid": pid, "born": born},
        {"kind": "inprocess", "pid": pid, "born": born + 1},
        {"kind": "inprocess", "pid": pid + 1, "born": born},
        {"kind": "process", "pid": pid, "born": born, "attempt": "separate-child"},
        {"kind": "inprocess", "pid": pid},
    ]
    members = []
    for index, record in enumerate(records):
        member = previous.add_agent(identity, team["id"], f"old-{index}", "default", str(tmp_path))
        previous.agent_status(identity, member["id"], "running", recovery=record)
        members.append(member["id"])
    previous.db.close()
    resumed = TeamStore(path)
    try:
        with pytest.raises(TeamError, match="live coordinator owner"):
            resumed.acquire_owner(identity, "foreign-token", handoff_pid=pid + 1)
        assert resumed.agent(identity, members[0])["recovery"] == records[0]
        resumed.acquire_owner(identity, "resumed-token", handoff_pid=pid)
        assert resumed.agent(identity, members[0])["recovery"]["kind"] == "absent"
        for member_id, record in zip(members[1:], records[1:], strict=True):
            assert resumed.agent(identity, member_id)["recovery"] == record
    finally:
        resumed.close()


@pytest.mark.timeout(30)
async def test_live_updater_parent_does_not_block_child_worker_cleanup(tmp_path: Path):
    identity = Identity("project", "root")
    path = TeamCoordinator(tmp_path, identity, {}).path
    previous = TeamStore(path)
    previous.acquire_owner(identity, "updater-token")
    team = previous.create_team(identity, "preserved")
    member = previous.add_agent(identity, team["id"], "old-task", "default", str(tmp_path))
    previous.agent_status(
        identity,
        member["id"],
        "running",
        recovery={"kind": "inprocess", "pid": os.getpid(), "born": psutil.Process().create_time()},
    )
    code = """
import asyncio, sys
from pathlib import Path
from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.models import Identity, TeamError

async def main():
    directory, parent, member = sys.argv[1:]
    identity = Identity('project', 'root')
    resumed = TeamCoordinator(Path(directory), identity, {}, handoff_pid=int(parent))
    try:
        await resumed._db('teams', identity)
        await resumed.cancel(identity, member)
        record = await resumed._db('agent', identity, member)
        assert record['status'] == 'stopped'
        duplicate = TeamCoordinator(Path(directory), identity, {})
        try:
            try:
                await duplicate._db('teams', identity)
            except TeamError as error:
                assert 'live coordinator owner' in str(error)
            else:
                raise AssertionError('Competing coordinator acquired the root')
        finally:
            await duplicate.close()
    finally:
        await resumed.close()

asyncio.run(main())
"""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            str(tmp_path),
            str(os.getpid()),
            member["id"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        assert process.returncode == 0, (stdout + stderr).decode(errors="replace")
        persisted = previous.agent(identity, member["id"])
        assert persisted["status"] == "stopped"
        assert persisted["recovery"]["kind"] == "absent"
        with pytest.raises(TeamError, match="ownership was lost"):
            previous.create_team(identity, "stale-updater-write")
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        previous.db.close()


def test_failed_handoff_keeps_old_owner_and_worker_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    identity = Identity("project", "root")
    path = tmp_path / "teams.sqlite3"
    previous = TeamStore(path)
    previous.acquire_owner(identity, "previous-token")
    team = previous.create_team(identity, "preserved")
    member = previous.add_agent(identity, team["id"], "old-task", "default", str(tmp_path))
    record = {"kind": "inprocess", "pid": os.getpid(), "born": psutil.Process().create_time()}
    previous.agent_status(identity, member["id"], "running", recovery=record)
    resumed = TeamStore(path)
    original = resumed._put

    def fail_after_write(caller: Identity, kind: str, value: Record) -> Never:
        original(caller, kind, value)
        message = "Simulated storage failure"
        raise OSError(message)

    monkeypatch.setattr(resumed, "_put", fail_after_write)
    try:
        with pytest.raises(OSError, match="storage failure"):
            resumed.acquire_owner(identity, "resumed-token", handoff_pid=os.getpid())
        assert previous.agent(identity, member["id"])["recovery"] == record
        # A write by the old owner succeeds only if its token survived rollback.
        previous.create_team(identity, "still-owned")
    finally:
        resumed.close()
        previous.close()
