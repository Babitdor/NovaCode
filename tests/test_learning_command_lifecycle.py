"""Commands expose exact revision decisions and recover legacy files safely."""

import json

import pytest

from novacode_cli.commands.learning_handler import handle_learning_review
from novacode_cli.hermes.activation import ActivationManager
from novacode_cli.hermes.ledger import LearningLedger


def test_shared_commands_pending_applied_rejected_failed_and_rollback(tmp_path):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    approved = ledger.propose(content="verify changes", project="p")
    rejected = ledger.propose(content="reject me", project="p")
    bad = ledger.propose(content="unsafe", kind="skill", target="../escape", project="p")
    other = ledger.propose(content="other project", project="other")
    args = dict(agent_dir=tmp_path, project="p")
    listing = handle_learning_review("review", **args)
    assert approved.reference in listing and "Intended change: verify changes" in listing
    assert other.reference not in listing
    assert "approved" in handle_learning_review("approve " + approved.reference, **args)
    assert "rejected" in handle_learning_review("reject " + rejected.reference, **args)
    handle_learning_review("approve " + bad.reference, **args)
    with pytest.raises(ValueError, match="does not belong"):
        handle_learning_review("approve " + other.reference, **args)
    with pytest.raises(ValueError, match="changed"):
        handle_learning_review("approve " + approved.reference, **args)
    ActivationManager(tmp_path, "p").apply_pending()
    listing = handle_learning_review("review", **args)
    assert all(status in listing for status in ("applied", "rejected", "failed"))
    applied = next(r for r in ledger.list() if r.id == approved.id)
    assert "rollback_pending" in handle_learning_review("rollback " + applied.reference, **args)
    ActivationManager(tmp_path, "p").apply_pending()
    assert "rolled_back" in handle_learning_review("review", **args)
    with pytest.raises(ValueError, match="Usage"):
        handle_learning_review("approve", **args)


def test_legacy_review_preserves_files_and_stages_only_recognized_generation(tmp_path, monkeypatch):
    import novacode_cli.prompts as prompts

    history = tmp_path / "overrides"
    templates = tmp_path / "templates"
    templates.mkdir()
    monkeypatch.setattr(prompts, "PROMPT_HISTORY_DIR", history)
    monkeypatch.setattr(prompts, "TEMPLATES_DIR", templates)
    generated = history / "generated"
    generated.mkdir(parents=True)
    (templates / "generated.jinja").write_text("base {{ value }}")
    (generated / "active.jinja").write_text("generated {{ value }}")
    (generated / "1.jinja").write_text("generated {{ value }}")
    (generated / "manifest.json").write_text(json.dumps({"active_version": 1}))
    manual = history / "manual"
    manual.mkdir()
    (manual / "active.jinja").write_text("manually owned")
    memory = tmp_path / "memories"
    memory.mkdir()
    lesson = memory / "lessons.md"
    lesson.write_text("Lessons captured during reviews / dreams.\nlegacy lesson")
    args = dict(agent_dir=tmp_path, project="p")
    listing = handle_learning_review("review", **args)
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    proposals = ledger.list()
    assert {record.kind for record in proposals} == {"prompt", "lesson"}
    assert all(record.status == "candidate" for record in proposals)
    assert str(manual / "active.jinja") in listing
    handle_learning_review("review", **args)
    assert len(ledger.list()) == 2
    assert (manual / "active.jinja").read_text() == "manually owned"
    assert lesson.read_text().endswith("legacy lesson")


def test_decision_export_failure_keeps_authoritative_approval(tmp_path, monkeypatch):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    record = ledger.propose(content="keep approval", project="p")

    def fail(*args, **kwargs):
        raise OSError("export fault")

    monkeypatch.setattr(LearningLedger, "export", fail)
    result = handle_learning_review("approve " + record.reference, agent_dir=tmp_path, project="p")
    assert "Ledger saved" in result and "export fault" in result
    assert ledger.list()[0].status == "approved"


@pytest.mark.asyncio
async def test_console_learning_routes_settings_and_revision_decisions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from novacode_cli.commands.learning_handler import handle_learning_command
    from novacode_cli.config import config, model_create, nova_config
    from novacode_cli.hermes import memory_tiers

    class Config:
        enabled = False

        def set_learning_enabled(self, enabled):
            self.enabled = enabled

        def get_learning_enabled(self):
            return self.enabled

    settings = Config()
    print_result = Mock()
    monkeypatch.setattr(nova_config, "NovaConfig", lambda: settings)
    monkeypatch.setattr(config.console, "print", print_result)
    monkeypatch.setattr(config.settings, "get_agent_dir", lambda _assistant: tmp_path)
    monkeypatch.setattr(memory_tiers, "project_memory_key", lambda _path: "p")
    monkeypatch.setattr(model_create, "create_model", lambda: "rebuilt-model")
    session = SimpleNamespace(switch_model=AsyncMock())
    ctx = SimpleNamespace(cmd_args="on", session_state=session, assistant_id="assistant")
    assert await handle_learning_command(ctx)
    assert settings.enabled is True
    session.switch_model.assert_awaited_once_with("rebuilt-model")
    ctx.cmd_args = "status"
    await handle_learning_command(ctx)
    assert "Learning on" in print_result.call_args.args[0]
    ctx.cmd_args = "off"
    ctx.session_state = None
    await handle_learning_command(ctx)
    assert settings.enabled is False
    record = LearningLedger(tmp_path / "learning.sqlite3").propose(content="review me", project="p")
    ctx.cmd_args = "approve " + record.reference
    await handle_learning_command(ctx)
    assert "approved" in print_result.call_args.args[0]
    ctx.cmd_args = "approve missing@1"
    await handle_learning_command(ctx)
    assert "does not belong" in print_result.call_args.args[0]
