from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys

import pytest

from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.hermes.activation import ActivationManager


def test_revision_approval_reject_rollback_and_project_isolation(tmp_path):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    first = ledger.propose(content="check migrations", project="a")
    other = ledger.propose(content="other", project="b")
    changed = ledger.revise(first.reference, "check migrations before applying")
    with pytest.raises(ValueError, match="changed"):
        ledger.transition(first.reference, "approve")
    approved = ledger.transition(changed.reference, "approve")
    assert ledger.list(project="a", active=True) == []
    ActivationManager(tmp_path, "a").apply_pending()
    applied = ledger.list(project="a", active=True)[0]
    assert applied.approval_reference == changed.reference
    assert ledger.list(project="b", active=True) == []
    ledger.transition(other.reference, "reject")
    ledger.transition(applied.reference, "rollback")
    ActivationManager(tmp_path, "a").apply_pending()
    assert ledger.list(project="a", active=True) == []
    assert len(ledger.history(first.id)) == 6


def test_shared_candidates_are_visible_for_review_but_not_retrieval(tmp_path):
    from novacode_cli.commands.learning_handler import handle_learning_review

    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    record = ledger.propose(content="global habit", scope="shared", kind="habit")
    assert record.reference in handle_learning_review("review", agent_dir=tmp_path, project="a")
    assert ledger.list(project="a", active=True) == []
    approved = ledger.transition(record.reference, "approve")
    assert ledger.list(project="a", active=True) == []
    ActivationManager(tmp_path, "a").apply_pending()
    applied = ledger.list(project="a", active=True)[0]
    assert applied.approval_reference == approved.approval_reference


def test_concurrent_connections_and_atomic_export(tmp_path):
    path = tmp_path / "learning.sqlite3"
    LearningLedger(path)

    def write(i):
        ledger = LearningLedger(path)
        record = ledger.propose(content=f"lesson {i}", project="a")
        ledger.transition(record.reference, "approve")
        ledger.export(tmp_path / "export.md", project="a")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))
    ledger = LearningLedger(path)
    ActivationManager(tmp_path, "a").apply_pending()
    ledger.export(tmp_path / "export.md", project="a")
    assert len(ledger.list(project="a", active=True)) == 20
    for i in range(20):
        assert f"lesson {i}" in (tmp_path / "export.md").read_text()


def test_legacy_import_is_unverified_idempotent_and_preserves_original(tmp_path):
    original = tmp_path / "legacy.md"
    original.write_text("legacy lesson")
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    assert ledger.import_legacy([original], project="a") == 1
    assert ledger.import_legacy([original], project="a") == 0
    assert ledger.list(project="a", active=True) == []
    assert original.read_text() == "legacy lesson"


def test_separate_process_writers_preserve_all_revisions(tmp_path):
    path = tmp_path / "learning.sqlite3"
    LearningLedger(path)
    code = "from pathlib import Path; import sys; from novacode_cli.hermes.ledger import LearningLedger; ledger=LearningLedger(Path(sys.argv[1])); record=ledger.propose(content=sys.argv[2],project='a'); ledger.transition(record.reference,'approve')"
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(path), str(i)]) for i in range(4)
    ]
    assert [process.wait(timeout=30) for process in processes] == [0] * 4
    ledger = LearningLedger(path)
    assert ledger.list(project="a", active=True) == []
    ActivationManager(tmp_path, "a").apply_pending()
    assert len(ledger.list(project="a", active=True)) == 4
    assert all(len(ledger.history(record.id)) == 3 for record in ledger.list())


def test_failed_revision_write_rolls_back_and_failed_export_preserves_file(tmp_path, monkeypatch):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    original = ledger.propose(content="lesson", project="a")
    insert = ledger._insert

    def failing_insert(db, record):
        insert(db, record)
        raise OSError("simulated failed write")

    monkeypatch.setattr(ledger, "_insert", failing_insert)
    with pytest.raises(OSError):
        ledger.transition(original.reference, "approve")
    assert ledger.history(original.id) == [original]
    monkeypatch.setattr(ledger, "_insert", insert)
    ledger.transition(original.reference, "approve")
    ActivationManager(tmp_path, "a").apply_pending()
    destination = tmp_path / "export.md"
    destination.write_text("prior complete export")

    def failed_replace(*args):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(type(destination), "replace", failed_replace)
    with pytest.raises(OSError):
        ledger.export(destination, project="a")
    assert destination.read_text() == "prior complete export"
    assert len(ledger.list(project="a", active=True)) == 1


def test_illegal_and_stale_state_transitions_cannot_mutate_ledger(tmp_path):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    with pytest.raises(ValueError, match="scope"):
        ledger.propose(content="unsafe", scope="unknown")
    record = ledger.propose(content="review first", project="p")
    for reference, action, error in [
        (record.id, "approve", "exact"),
        (record.reference, "unknown", "Unknown learning"),
        ("missing@1", "approve", "Unknown proposal"),
        (record.reference, "rollback", "active knowledge"),
        (record.reference, "apply", "Invalid application"),
    ]:
        with pytest.raises(ValueError, match=error):
            ledger.transition(reference, action)
    assert ledger.history(record.id) == [record]
    with pytest.raises(ValueError, match="Unknown proposal"):
        ledger.revise("missing@1", "changed")
    with pytest.raises(ValueError, match="Stale"):
        ledger.revise(record.id + "@99", "changed")
    approved = ledger.transition(record.reference, "approve")
    for action in ("approve", "reject"):
        with pytest.raises(ValueError, match="candidate"):
            ledger.transition(approved.reference, action)
    assert len(ledger.history(record.id)) == 2


def test_an_auto_approved_proposal_is_an_ordinary_approval(tmp_path):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    waiting = ledger.propose(content="by hand", project="a")
    auto = ledger.propose(content="use cmd.exe syntax", project="a", auto_approve=True)

    assert waiting.status == "candidate"
    assert auto.status == "approved" and auto.approval_reference.endswith("@1")
    ActivationManager(tmp_path, "a").apply_pending()
    applied = ledger.list(project="a", active=True)
    assert [r.content for r in applied] == ["use cmd.exe syntax"]
    ledger.transition(applied[0].reference, "rollback")  # undone like any approval
    ActivationManager(tmp_path, "a").apply_pending()
    assert ledger.list(project="a", active=True) == []


def test_reviews_auto_approve_lessons_but_never_prompt_suggestions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from novacode_cli.hermes.review import ReviewRunner

    monkeypatch.setattr("novacode_cli.hermes.ledger.auto_approve_enabled", lambda: True)
    runner = SimpleNamespace(
        _agent_dir=tmp_path, _project="p", _outcome=("success",),
        _tracker=SimpleNamespace(owner="s1", task="t1"),
    )
    parsed = {"lessons": [{"bullets": ["- run tests with -q"], "topic": "testing"}]}
    response = "<prompt_improvement>Always greet the user.</prompt_improvement>"

    ReviewRunner._persist_review(runner, parsed, response)

    status = {r.content: r.status for r in LearningLedger(tmp_path / "learning.sqlite3").list()}
    assert status == {"- run tests with -q": "approved", "Always greet the user.": "candidate"}
