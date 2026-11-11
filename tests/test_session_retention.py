"""Retention sweep for ~/.nova/sessions/ — the store nothing used to prune.

``_cleanup_old_checkpoints`` bounds the checkpoint DBs, but each session dir
holds a full ``archive.jsonl`` plus ``large_tool_results/`` and
``conversation_history/``, so without a sweep the session tree is the largest
thing Nova writes and never shrinks.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from novacode_cli.session.session_persistence import SessionManager

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_session(root: Path, name: str, *, age_days: float, nested: bool = False) -> Path:
    """Create a session dir whose newest file is ``age_days`` old."""
    d = root / name
    d.mkdir(parents=True)
    (d / "archive.jsonl").write_text('{"role": "user"}\n', encoding="utf-8")
    if nested:
        # A nested artifact must count as activity, or an actively-used session
        # whose top-level files are old would be reaped.
        sub = d / "large_tool_results"
        sub.mkdir()
        (sub / "abc").write_text("x", encoding="utf-8")
        target = sub / "abc"
    else:
        target = d / "archive.jsonl"
    ts = time.time() - age_days * 86_400
    os.utime(target, (ts, ts))
    os.utime(d / "archive.jsonl", (ts, ts))
    return d


def test_reaps_only_sessions_past_the_cutoff(tmp_path: Path) -> None:
    old = _make_session(tmp_path, "old", age_days=45)
    fresh = _make_session(tmp_path, "fresh", age_days=1)

    removed = SessionManager(sessions_dir=tmp_path).cleanup_old_sessions(max_age_days=30)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists(), "a session inside the retention window must survive"


def test_newest_nested_file_keeps_a_session_alive(tmp_path: Path) -> None:
    """Age is the newest file anywhere in the dir, not the dir's own mtime."""
    kept = _make_session(tmp_path, "active", age_days=60, nested=True)
    # Only the nested artifact is recent — the top-level archive is 60 days old.
    ts = time.time()
    os.utime(kept / "large_tool_results" / "abc", (ts, ts))

    removed = SessionManager(sessions_dir=tmp_path).cleanup_old_sessions(max_age_days=30)

    assert removed == 0
    assert kept.exists()


def test_current_session_is_never_reaped(tmp_path: Path) -> None:
    live = _make_session(tmp_path, "live", age_days=90)

    removed = SessionManager(sessions_dir=tmp_path).cleanup_old_sessions(
        max_age_days=30, keep_session_id="live"
    )

    assert removed == 0
    assert live.exists()


def test_disabled_when_cutoff_is_non_positive(tmp_path: Path) -> None:
    old = _make_session(tmp_path, "old", age_days=99)

    assert SessionManager(sessions_dir=tmp_path).cleanup_old_sessions(max_age_days=0) == 0
    assert old.exists()


# ── Durable checkpoint pruning ───────────────────────────────────────────────


_CHECKPOINT_DDL = """
CREATE TABLE checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    value BLOB,
    task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""


def _insert_checkpoint(
    db_path: Path, thread_id: str, checkpoint_ns: str, checkpoint_id: str, blob: bytes
) -> None:
    import sqlite3

    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,"
        " type, checkpoint, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (thread_id, checkpoint_ns, checkpoint_id, None, "checkpoint", sqlite3.Binary(blob), "{}"),
    )
    con.commit()
    con.close()


def _insert_write(
    db_path: Path,
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str,
    task_id: str,
    value: bytes,
) -> None:
    import sqlite3

    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type,"
        " value, task_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            thread_id,
            checkpoint_ns,
            checkpoint_id,
            task_id,
            0,
            "value",
            "state",
            sqlite3.Binary(value),
            "",
        ),
    )
    con.commit()
    con.close()


def test_prune_keeps_only_the_newest_checkpoint_per_thread(tmp_path: Path) -> None:
    from novacode_cli.main import _prune_checkpoints

    db_path = tmp_path / "nova_checkpoints.db"
    import sqlite3

    con = sqlite3.connect(db_path)
    con.executescript(_CHECKPOINT_DDL)
    _insert_checkpoint(db_path, "thread-a", "", "checkpoint-1", b"x" * 10)
    _insert_checkpoint(db_path, "thread-a", "", "checkpoint-2", b"x" * 20)
    _insert_checkpoint(db_path, "thread-b", "", "checkpoint-1", b"x" * 30)
    _insert_write(db_path, "thread-a", "", "checkpoint-1", "task-1", b"stale-write")
    _insert_write(db_path, "thread-a", "", "checkpoint-2", "task-1", b"live-write")
    con.commit()
    con.close()

    assert _prune_checkpoints(db_path) == 1

    con = sqlite3.connect(db_path)
    kept_checkpoints = con.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints ORDER BY checkpoint_id"
    ).fetchall()
    kept_writes = con.execute("SELECT checkpoint_id FROM writes").fetchall()
    con.close()

    assert kept_checkpoints == [
        ("thread-b", "", "checkpoint-1"),
        ("thread-a", "", "checkpoint-2"),
    ]
    assert kept_writes == [("checkpoint-2",)]


def test_prune_keeps_the_latest_checkpoint_per_namespace(tmp_path: Path) -> None:
    from novacode_cli.main import _prune_checkpoints

    db_path = tmp_path / "nova_checkpoints.db"
    import sqlite3

    con = sqlite3.connect(db_path)
    con.executescript(_CHECKPOINT_DDL)
    _insert_checkpoint(db_path, "thread-a", "", "checkpoint-1", b"x" * 10)
    _insert_checkpoint(db_path, "thread-a", "tools:task-1", "tool-checkpoint-1", b"y" * 5)
    _insert_checkpoint(db_path, "thread-a", "tools:task-1", "tool-checkpoint-2", b"y" * 6)
    con.commit()
    con.close()

    assert _prune_checkpoints(db_path) == 1

    con = sqlite3.connect(db_path)
    kept = con.execute("SELECT checkpoint_ns, checkpoint_id FROM checkpoints").fetchall()
    con.close()
    assert kept == [("", "checkpoint-1"), ("tools:task-1", "tool-checkpoint-2")]


def test_prune_is_noop_for_a_database_without_the_checkpointer_schema(tmp_path: Path) -> None:
    from novacode_cli.main import _prune_checkpoints

    db_path = tmp_path / "fresh.db"
    import sqlite3

    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE unrelated (id INTEGER)")
    con.commit()
    con.close()

    assert _prune_checkpoints(db_path) == 0


def test_prune_does_not_vacuum_large_database_without_stale_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from novacode_cli import main

    db_path = tmp_path / "nova_checkpoints.db"
    con = sqlite3.connect(db_path)
    con.executescript(_CHECKPOINT_DDL)
    con.close()
    _insert_checkpoint(db_path, "thread-a", "", "checkpoint-1", b"live")

    real_connect = sqlite3.connect
    executed: list[str] = []

    def trace(statement: str) -> None:
        if statement.strip().upper() == "VACUUM":
            executed.append(statement)

    def tracked_connect(
        database: str | Path, *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        connection = real_connect(database, *args, **kwargs)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(main, "_CHECKPOINT_COMPACT_SIZE_BYTES", 0)

    assert main._prune_checkpoints(db_path) == 0
    assert executed == []
