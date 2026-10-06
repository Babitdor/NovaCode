"""Durable snapshots survive interrupted compatibility writes and process exit."""

import json
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from novacode_cli.session.session_persistence import SessionManager


def test_snapshot_survives_failed_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = SessionManager(tmp_path)
    manager.save_session("s", "t", [HumanMessage("old")], "nova")
    original = Path.replace

    def fail_snapshot(source: Path, target: Path) -> Path:
        if Path(target).name == "snapshot.json":
            message = "disk unavailable"
            raise OSError(message)
        return original(source, target)

    monkeypatch.setattr(Path, "replace", fail_snapshot)
    with pytest.raises(OSError, match="disk unavailable"):
        manager.save_session("s", "t", [HumanMessage("new")], "nova")
    assert [m.content for m in manager.load_session("s").messages] == ["old"]


def test_snapshot_ignores_torn_compatibility_files(tmp_path: Path):
    manager = SessionManager(tmp_path)
    messages = [HumanMessage(f"prompt-{i}") for i in range(22)] + [AIMessage("answer")]
    directory = manager.save_session("s", "t", messages, "nova", todos=[{"task": "work"}])
    (directory / "recent.jsonl").write_text('{"type":', encoding="utf-8")
    (directory / "archive.jsonl").write_text("", encoding="utf-8")
    restored = manager.load_session("s")
    assert [m.content for m in restored.messages] == [m.content for m in messages]
    assert restored.todos == [{"task": "work"}]


def test_abrupt_process_exit_keeps_resumable_session(tmp_path: Path):
    code = """
import os, sys
from pathlib import Path
from langchain_core.messages import HumanMessage
from novacode_cli.session.session_persistence import SessionManager
SessionManager(Path(sys.argv[1])).save_session(
    'crash', 'thread', [HumanMessage('recover me')], 'nova', task_status='interrupted'
)
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)  # noqa: S603 — fixed test program
    assert result.returncode == 17
    manager = SessionManager(tmp_path)
    assert manager.list_sessions()[0].session_id == "crash"
    assert manager.load_session("crash").messages[0].content == "recover me"


def test_empty_crash_save_keeps_last_conversation(tmp_path: Path):
    manager = SessionManager(tmp_path)
    manager.save_session("s", "t", [HumanMessage("keep")], "nova")
    manager.save_session("s", "t", [], "nova", task_status="crashed")
    assert manager.load_session("s").messages[0].content == "keep"
    assert manager.load_session("s").meta.task_status == "crashed"


def test_snapshot_committed_before_first_metadata_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import novacode_cli.session.session_persistence as persistence

    original = persistence.atomic_write

    def fail_metadata(path: Path, content: str) -> None:
        if path.name == "meta.json":
            message = "process exited before metadata"
            raise OSError(message)
        original(path, content)

    manager = SessionManager(tmp_path)
    monkeypatch.setattr(persistence, "atomic_write", fail_metadata)
    with pytest.raises(OSError, match="process exited"):
        manager.save_session("first", "thread", [HumanMessage("durable")], "nova")
    assert manager.list_sessions()[0].session_id == "first"
    assert manager.load_session("first").messages[0].content == "durable"
    assert manager.load_recent_messages("first")[0].content == "durable"


def test_generated_conversations_round_trip(tmp_path: Path):
    """Ordering/content survive varied sizes, Unicode, newlines and JSON characters."""
    generator = random.Random(19)
    manager = SessionManager(tmp_path)
    alphabet = '日本語✓\n\r\\"<> &\x00abc'
    for count in [0, 1, 19, 20, 21, 60]:
        messages = [
            (HumanMessage if index % 2 == 0 else AIMessage)(
                "".join(generator.choices(alphabet, k=generator.randrange(1, 90)))
            )
            for index in range(count)
        ]
        manager.save_session("generated", "thread", messages, "nova", todos=[])
        restored = manager.load_session("generated")
        assert [(m.type, m.content) for m in restored.messages] == [
            (m.type, m.content) for m in messages
        ]
        assert restored.meta.message_count == count
        assert restored.todos == []
        snapshot = json.loads(
            (tmp_path / "generated" / "snapshot.json").read_text(encoding="utf-8")
        )
        assert snapshot["meta"]["message_count"] == len(snapshot["messages"])


def test_concurrent_saves_commit_matching_files(tmp_path: Path):
    manager = SessionManager(tmp_path)

    def save(index: int) -> None:
        manager.save_session("shared", "thread", [HumanMessage(str(index))] * (index + 1), "nova")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(save, range(12)))
    directory = tmp_path / "shared"
    snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    recent = [
        json.loads(line)
        for line in (directory / "recent.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert meta == snapshot["meta"]
    assert len(recent) == meta["message_count"]
    assert recent == snapshot["messages"]


def test_metadata_follows_snapshot_when_compatibility_metadata_lags(tmp_path: Path):
    manager = SessionManager(tmp_path)
    directory = manager.save_session(
        "s", "thread", [HumanMessage("old")], "nova", model_name="old-model"
    )
    old_meta = (directory / "meta.json").read_text(encoding="utf-8")
    manager.save_session(
        "s", "thread", [HumanMessage("old"), AIMessage("new")], "nova", model_name="new-model"
    )
    (directory / "meta.json").write_text(old_meta, encoding="utf-8")
    assert manager.load_session_meta("s").model_name == "new-model"
    assert manager.list_sessions()[0].message_count == 2


def test_legacy_state_migrates_into_recovery_snapshot(tmp_path: Path):
    manager = SessionManager(tmp_path)
    directory = manager.save_session(
        "legacy",
        "thread",
        [HumanMessage("earlier")],
        "nova",
        todos=[{"task": "continue"}],
        tool_state={"tool": "output"},
        memory="remember this",
        workspace_state={"cwd": "workspace"},
        shared_memory={"decision": "agreed"},
    )
    (directory / "snapshot.json").unlink()
    assert manager.load_session("legacy").memory == "remember this"
    manager.save_session("legacy", "thread", [HumanMessage("later")], "nova")
    restored = manager.load_session("legacy")
    assert restored.memory == "remember this"
    assert restored.todos == [{"task": "continue"}]
    assert restored.tool_state == {"tool": "output"}
    assert restored.workspace_state == {"cwd": "workspace"}
    assert restored.shared_memory == {"decision": "agreed"}
