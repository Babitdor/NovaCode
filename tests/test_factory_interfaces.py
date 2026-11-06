"""Factory controls remain explicit and preserve unrelated session configuration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Static

from novacode_cli.commands.prime_handler import factory_receipt, handle_prime_command
from novacode_cli.config.nova_config import FACTORY_ROLES, ROLE_NAMES, NovaConfig
from novacode_cli.prime import prime_payload, receipt, restore_prime
from novacode_cli.tui.prime_panel import PrimePanel

if TYPE_CHECKING:
    from pathlib import Path


def session() -> SimpleNamespace:
    state = SimpleNamespace(
        thread_id="factory-session",
        prime_enabled=True,
        auto_approve=True,
        prime_factory=Mock(),
        prime_pending=None,
        prime_report={
            "version": 2,
            "factory": True,
            "id": "factory-run",
            "thread": "factory-session",
            "goal": "Repair the parser",
            "stage": "plan_approval",
            "status": "waiting",
            "tasks": [{"id": "requirements", "role": "requirements", "status": "done"}],
            "sources": [{"url": "https://docs.example.test/v1", "version": "1"}],
            "checks": [{"id": "acceptance", "status": "unverified"}],
            "approvals": [{"kind": "plan", "status": "pending"}],
            "usage": {"tokens": 42, "active_seconds": 3},
            "budget": {"tokens": 500000, "active_seconds": 3600},
            "blockers": ["Plan requires owner approval"],
            "candidate": None,
        },
    )
    state.prime_factory.record = state.prime_report
    return state


def test_off_pauses_and_resume_never_grants_approval():
    state = session()
    report = state.prime_report.copy()
    assert "off" in handle_prime_command(state, "off")
    state.prime_factory.request_pause.assert_called_once_with()
    assert not state.prime_enabled
    assert "revalidated" in handle_prime_command(state, "resume factory-run")
    assert state.prime_factory_resume == "factory-run"
    assert state.prime_enabled
    assert state.prime_report == {**report, "status": "paused"}
    assert state.auto_approve


def test_control_commands_and_inspection_do_not_start_work():
    state = session()
    assert "requested" in handle_prime_command(state, "cancel")
    state.prime_factory.request_cancel.assert_called_once_with()
    assert "requested" in handle_prime_command(state, "pause")
    state.prime_factory.request_pause.assert_called_once_with()
    assert "requirements" in handle_prime_command(state, "agents")
    assert "docs.example.test" in handle_prime_command(state, "sources")
    assert "Factory" in handle_prime_command(state, "report")
    assert "Factory" in handle_prime_command(state, "status")
    assert not hasattr(state, "prime_factory_resume")


@pytest.mark.parametrize("value", ["0 5", "30 nan", "30 inf", "-1 2", "x 2", "4 0"])
def test_invalid_budget_never_reaches_controller(value: str) -> None:
    state = session()
    assert "positive" in handle_prime_command(state, "budget " + value)
    state.prime_factory.set_budget.assert_not_called()


def test_budget_uses_active_seconds():
    state = session()
    assert "750000" in handle_prime_command(state, "budget 750000 90")
    state.prime_factory.set_budget.assert_called_once_with(750000, 5400)


def test_factory_models_are_isolated_and_corruption_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "novacode_cli.config.nova_config.Settings.from_environment",
        lambda: SimpleNamespace(user_deepagents_dir=tmp_path),
    )
    config = NovaConfig()
    assert config.config_dir == tmp_path
    assert all(config.get_factory_model(role) is None for role in FACTORY_ROLES)
    assert ROLE_NAMES == ("main", "subagent", "async", "dynamic")
    config.set_role_model("subagent", "openai", "ordinary")
    config.set_factory_model("developer", "openai", "factory", "https://example.test")
    assert config.get_role_model("subagent")["model"] == "ordinary"
    assert config.get_factory_model("developer")["model"] == "factory"
    assert config.get_factory_model("research") is None
    config._config["factory_model_security"] = {"provider": "openai"}
    config._save()
    with pytest.raises(ValueError, match="Invalid factory model"):
        config.get_factory_model("security")
    config.clear_factory_model("developer")
    assert config.get_factory_model("developer") is None
    with pytest.raises(ValueError, match="Unknown factory role"):
        config.set_factory_model("main", "openai", "wrong")


def test_receipt_preserves_blockers_and_unknown_costs():
    report = factory_receipt(session().prime_report)
    assert "42 / 500000" in report
    assert "Plan requires owner approval" in report
    assert "unverified" in report
    assert "Costs are unknown" in report


@pytest.mark.timeout(30)
async def test_factory_dashboard_displays_host_state():
    class FactoryApp(App):
        def compose(self) -> ComposeResult:
            yield PrimePanel(id="prime")

    state = session()
    app = FactoryApp()
    async with app.run_test(size=(100, 30)):
        panel = app.query_one(PrimePanel)
        panel.update_report(state)
        label = str(panel.query_one("#prime-strip", Button).label)
        assert "Factory" in label
        assert "plan_approval" in label
        assert "42/500000" in label
        assert "1 blockers" in label
        await panel.show_details(state, view="progress")
        assert "owner approval" in str(panel.query_one("#prime-details", Static).render())


def test_factory_snapshot_restores_paused_without_approvals_or_automatic_resume():
    state = session()
    saved = prime_payload(state)
    state.prime_report["approvals"] = {"plan": {"digest": "old"}}
    other = SimpleNamespace(thread_id=state.thread_id)
    restore_prime(other, saved)
    assert other.prime_report["status"] == "paused"
    assert other.prime_report["approvals"] == {}
    assert other.prime_factory_resume is None
    assert "Software Factory" in receipt(other.prime_report)
    assert other.prime_pending is None


def test_factory_store_browsing_and_explicit_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from novacode_cli.prime_factory.store import FactoryStore
    from novacode_cli.tui.prime_panel import _saved_records

    path = tmp_path / "factory.sqlite3"
    monkeypatch.setattr("novacode_cli.prime_factory.controller.storage_path", lambda _: path)
    state = session()
    store = FactoryStore(path)
    record = {**state.prime_report, "id": "older-factory"}
    store.save(record)
    store.close()
    state.prime_report = None
    assert "older-factory" in handle_prime_command(state, "tasks")
    rows = _saved_records(state, "tasks")
    assert rows[0]["id"] == "older-factory"
    assert "revalidated" in handle_prime_command(state, "resume older-factory")
    assert state.prime_factory_resume == "older-factory"
    assert state.prime_report["status"] == "paused"
    assert state.prime_report["thread"] == state.thread_id


def test_saved_budget_can_be_changed_without_resuming_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from novacode_cli.prime_factory import ROLES
    from novacode_cli.prime_factory.controller import FactoryController
    from novacode_cli.prime_factory.store import FactoryStore

    path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr("novacode_cli.prime_factory.controller.storage_path", lambda _: path)
    state = SimpleNamespace(
        thread_id="saved",
        prime_enabled=True,
        prime_factory=None,
        working_directory=str(tmp_path),
        auto_approve=True,
    )
    controller = FactoryController.create(
        tmp_path,
        "Fix a bug",
        state.thread_id,
        {role: {"provider": "offline", "model": "scripted"} for role in ROLES},
        session=state,
        store=FactoryStore(path),
    )
    controller.request_pause()
    controller.store.close()
    assert "budget set" in handle_prime_command(state, "budget 600000 90")
    store = FactoryStore(path)
    try:
        record = store.load(state.prime_report["id"])
        assert record["budget"] == {"tokens": 600000, "active_seconds": 5400}
        assert record["status"] == "paused"
        assert state.auto_approve is True
    finally:
        store.close()
