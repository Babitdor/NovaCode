"""Worker usage stays separate from a finished parent turn and counts once."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from novacode_cli.teams.models import Identity, TeamError
from novacode_cli.teams.store import TeamStore
from novacode_cli.tracking.usage_tree import UsageTree

if TYPE_CHECKING:
    from pathlib import Path


def test_worker_usage_deduplicates_and_preserves_unknown_prices(tmp_path: Path):
    store = TeamStore(tmp_path / "teams.sqlite3")
    lead = Identity("project", "root")
    team = store.create_team(lead, "team")
    agent = store.add_agent(lead, team["id"], "worker", "default", str(tmp_path))
    tree = UsageTree(agent["id"])
    tree.record(
        ("main",),
        "unknown-provider-model",
        {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 80},
    )
    for _ in range(2):
        store.record_usage(lead, agent["id"], "unique-turn", tree.to_dict())
    result = store.agent(lead, agent["id"])
    assert result["usage_totals"]["input_tokens"] == 100
    assert result["usage_totals"]["cache_read_tokens"] == 80
    assert result["usage_totals"]["calls"] == 1
    assert result["usage_cost_usd"] is None
    tree.record(("main",), "unknown-provider-model", {})
    with pytest.raises(TeamError, match="replay"):
        store.record_usage(lead, agent["id"], "unique-turn", tree.to_dict())
    store.close()


def test_missing_usage_remains_unavailable(tmp_path: Path):
    store = TeamStore(tmp_path / "teams.sqlite3")
    lead = Identity("project", "root")
    team = store.create_team(lead, "team")
    agent = store.add_agent(lead, team["id"], "worker", "fork", str(tmp_path))
    result = store.record_usage(lead, agent["id"], "turn", {"availability": "unavailable"})
    assert result["usage_cost_usd"] is None
    assert result["usage_cost_unknown"]
    store.close()
