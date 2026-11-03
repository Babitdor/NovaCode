"""Update restarts may transfer a root lease from their own prior process."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from novacode_cli.teams.models import Identity, TeamError
from novacode_cli.teams.store import TeamStore

if TYPE_CHECKING:
    from pathlib import Path


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
