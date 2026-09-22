"""Retention sweep for ~/.nova/sessions/ — the store nothing used to prune.

``_cleanup_old_checkpoints`` bounds the checkpoint DBs, but each session dir
holds a full ``archive.jsonl`` plus ``large_tool_results/`` and
``conversation_history/``, so without a sweep the session tree is the largest
thing Nova writes and never shrinks.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from novacode_cli.session.session_persistence import SessionManager


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
