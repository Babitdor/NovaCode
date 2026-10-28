"""Evolution reflects the current ledger without activating or creating records."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from novacode_cli.hermes.ledger import LearningLedger

if TYPE_CHECKING:
    from pathlib import Path


def test_read_activity_does_not_create_a_missing_ledger(tmp_path: Path):
    path = tmp_path / "missing" / "learning.sqlite3"
    assert LearningLedger.read_activity(path) == []
    assert not path.parent.exists()


def test_read_activity_returns_current_revision_without_writes(tmp_path: Path):
    path = tmp_path / "learning.sqlite3"
    ledger = LearningLedger(path)
    pending = ledger.propose(content="Pending lesson", kind="lesson")
    approved = ledger.propose(content="Approved skill", kind="skill", target="checks")
    approved = ledger.transition(approved.reference, "approve")
    before = path.read_bytes()
    rows = LearningLedger.read_activity(path)
    assert {r.id: r.status for r, _ in rows} == {pending.id: "candidate", approved.id: "approved"}
    assert next(ts for r, ts in rows if r.id == approved.id) > 0
    assert path.read_bytes() == before


def test_activity_uses_the_selected_agent_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.commands.evolution_handler import _learning_activity
    from novacode_cli.config.config import Settings

    monkeypatch.setattr("novacode_cli.config.config.HOME_DIR", tmp_path / "nova")
    monkeypatch.setattr(Settings, "get_agent_dir", lambda _self, name: tmp_path / name)
    LearningLedger(tmp_path / "nova-agent" / "learning.sqlite3").propose(
        content="main", target="main"
    )
    custom = LearningLedger(tmp_path / "custom" / "learning.sqlite3").propose(
        content="custom", target="custom"
    )
    _, entries = _learning_activity("custom")
    assert [entry["reference"] for entry in entries] == [custom.reference]


@pytest.mark.asyncio
async def test_loader_combines_current_proposals_and_labelled_legacy_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.commands.evolution_handler import _load_evolution
    from novacode_cli.config.config import Settings
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr("novacode_cli.config.config.HOME_DIR", tmp_path / "nova")

    monkeypatch.setattr(Settings, "get_agent_dir", lambda _self, _name: tmp_path)
    cfg = NovaConfig()
    cfg.set_learning_enabled(False)
    cfg.set_learning_auto_approve(False)
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    pending = ledger.propose(content="Run repository checks", kind="skill", target="checks")
    applied = ledger.propose(content="Use the formatter", kind="lesson")
    applied = ledger.transition(applied.reference, "approve")
    applied = ledger.transition(applied.reference, "apply")
    store = SimpleNamespace(
        aget=AsyncMock(return_value=SimpleNamespace(value={"unlocked": 5, "leveled": 2})),
        asearch=AsyncMock(
            return_value=[SimpleNamespace(value={"kind": "unlock", "skill": "old", "ts": 1})]
        ),
    )
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    before = (tmp_path / "learning.sqlite3").read_bytes()
    counters, entries = await _load_evolution(strict=True)
    assert counters["candidate"] == counters["applied"] == 1
    assert counters["learning_enabled"] == counters["auto_approve"] == 0
    assert entries[0]["source"] == "ledger"
    assert {e["reference"] for e in entries if e["source"] == "ledger"} == {
        pending.reference,
        applied.reference,
    }
    assert entries[-1]["status"] == "historical"
    assert counters["unlocked"] == 5
    assert (tmp_path / "learning.sqlite3").read_bytes() == before
    assert NovaConfig().get_learning_enabled() is False
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_dashboard_refreshes_ledger_state_without_applying_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from textual.widgets import Select, Static

    from novacode_cli.config.config import Settings
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.tui.reference_screens import EvolutionScreen
    from tests.test_reference_screens import Host

    monkeypatch.setattr("novacode_cli.config.config.HOME_DIR", tmp_path / "nova")
    monkeypatch.setattr(Settings, "get_agent_dir", lambda _self, _name: tmp_path)
    cfg = NovaConfig()
    cfg.set_learning_enabled(False)
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    record = ledger.propose(content="[red]literal proposal", kind="skill", target="checks")
    store = SimpleNamespace(aget=AsyncMock(return_value=None), asearch=AsyncMock(return_value=[]))
    monkeypatch.setattr("novacode_cli.memory.store.get_durable_store", lambda: store)
    panel = EvolutionScreen()
    async with Host(panel).run_test(size=(80, 30)) as pilot:
        await panel.workers.wait_for_complete()
        assert "Learning off" in str(panel.query_one("#evolution-totals", Static).content)
        panel.query_one(Select).value = "candidate"
        await pilot.pause()
        assert [e["reference"] for e in panel.visible_entries] == [record.reference]
        assert "Pending review" in str(panel.query_one("#reference-detail", Static).content)
        assert ledger.list()[0].status == "candidate"
        approved = ledger.transition(record.reference, "approve")
        ledger.transition(approved.reference, "apply")
        await pilot.click("#evolution-refresh")
        await panel.workers.wait_for_complete()
        assert panel.visible_entries == []
        panel.query_one(Select).value = "applied"
        await pilot.pause()
        assert len(panel.visible_entries) == 1
        assert "Applied" in str(panel.query_one("#reference-detail", Static).content)
        assert "[red]literal proposal" in str(panel.query_one("#reference-detail", Static).content)
    assert NovaConfig().get_learning_enabled() is False
    assert not (tmp_path / "skills").exists()
