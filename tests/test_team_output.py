"""Large result previews retain exact, immutable text within explicit disk limits."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.models import Identity, TeamError
from novacode_cli.teams.output import persist_output


async def test_large_result_is_bounded_with_exact_recovery(tmp_path: Path):
    adapter = AsyncMock()
    adapter.start.return_value.stop.return_value = True
    host = TeamCoordinator(tmp_path, Identity("p", "r"), {"default": adapter})
    team = await host.request(host.lead, "team_create", {"name": "results"})
    agent = await host.spawn(host.lead, "verify", team["id"])
    text = "exact é中\n" * 20_000
    await host.on_event(agent["id"], "idle", {"full_output": text})
    result = await host.request(host.lead, "get_agent", {"agent_id": agent["id"]})
    assert len(result["summary"].encode("utf-8")) <= 60_000
    contents = await asyncio.to_thread(
        Path(result["output_artifact"]["path"]).read_text, encoding="utf-8"
    )
    assert contents == text
    assert "full_output" not in result
    await host.close()


def test_existing_artifact_cannot_silently_return_changed_bytes(tmp_path: Path):
    result = persist_output(tmp_path, "exact")
    target = Path(result["path"])
    target.write_text("other", encoding="utf-8")
    with pytest.raises(TeamError, match="verification"):
        persist_output(tmp_path, "exact")
    assert target.read_text(encoding="utf-8") == "other"


def test_artifact_budget_preserves_existing_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.teams import output

    first = persist_output(tmp_path, "first")
    monkeypatch.setattr(output, "MAX_ARTIFACT_BYTES", 5)
    with pytest.raises(TeamError, match="budget"):
        persist_output(tmp_path, "second")
    assert Path(first["path"]).read_text(encoding="utf-8") == "first"
