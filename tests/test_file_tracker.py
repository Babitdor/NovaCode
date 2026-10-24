"""Tests for FileTrackerMiddleware's read-before-edit gate.

The gate prevents edits to files the agent hasn't seen, but a file the agent
*wrote* this session is known content too — gating it falsely rejected the very
common write_file→edit_file pattern. These lock in the relaxed behavior.
"""

from __future__ import annotations

import types

from novacode_cli.tracking.file_tracker import (
    FileTrackerMiddleware,
    get_session_tracker,
    reset_session_tracker,
)


def _req(name: str, args: dict):
    return types.SimpleNamespace(tool_call={"name": name, "args": args, "id": "tc1"})


def _edit(path: str):
    return _req("edit_file", {"file_path": path, "old_string": "a", "new_string": "b"})


def test_edit_on_unseen_file_is_rejected_as_error():
    reset_session_tracker()
    mw = FileTrackerMiddleware()
    rejection = mw._check_edit_allowed(_edit("/unseen.py"))
    assert rejection is not None
    # Surfaced as an error so the UI shows a failed edit, not a silent no-diff ✓.
    assert getattr(rejection, "status", None) == "error"


def test_edit_after_read_is_allowed():
    reset_session_tracker()
    mw = FileTrackerMiddleware()
    get_session_tracker().record_read("/bar.py", "content")
    assert mw._check_edit_allowed(_edit("/bar.py")) is None


def test_edit_after_write_is_allowed():
    # The regression: writing a file then editing it must not be rejected.
    reset_session_tracker()
    mw = FileTrackerMiddleware()
    get_session_tracker().record_write("/foo.py", "new content", operation="write")
    assert mw._check_edit_allowed(_edit("/foo.py")) is None


def test_gate_disabled_allows_everything():
    reset_session_tracker()
    mw = FileTrackerMiddleware(enforce_read_before_edit=False)
    assert mw._check_edit_allowed(_edit("/whatever.py")) is None



def _old_file(tmp_path, name: str = "old.py"):
    import os

    path = tmp_path / name
    path.write_text("line 1\nline 2\n", encoding="utf-8")
    os.utime(path, (1_000_000, 1_000_000))  # existed long before this session
    return path


def _write(path: str):
    return _req("write_file", {"file_path": path, "content": "replaced"})


def test_overwrite_of_unread_preexisting_file_is_refused(tmp_path):
    reset_session_tracker()
    _old_file(tmp_path)
    mw = FileTrackerMiddleware(enforce_read_before_edit=False, workspace_root=str(tmp_path))
    rejection = mw._check_edit_allowed(_write("/old.py"))
    assert rejection is not None
    assert rejection.status == "error"
    assert "has not been read" in rejection.content
    assert mw._check_edit_allowed(_write("/new.py")) is None


def test_overwrite_after_partial_read_names_the_partial_view(tmp_path):
    reset_session_tracker()
    _old_file(tmp_path)
    mw = FileTrackerMiddleware(enforce_read_before_edit=False, workspace_root=str(tmp_path))
    get_session_tracker().record_read("/old.py", "1 line 1", 0, 1, partial=True)
    rejection = mw._check_edit_allowed(_write("old.py"))
    assert rejection is not None
    assert "Only part of this file has been read" in rejection.content
    get_session_tracker().record_read("/old.py", "1 line 1\n2 line 2")
    assert mw._check_edit_allowed(_write("old.py")) is None
    # A later partial re-read does not undo having seen the whole file.
    get_session_tracker().record_read("/old.py", "2 line 2", 1, 1, partial=True)
    assert mw._check_edit_allowed(_write("/old.py")) is None


def test_overwrite_allowed_for_session_files_and_without_a_root(tmp_path):
    reset_session_tracker()
    (tmp_path / "fresh.py").write_text("made by a command this session", encoding="utf-8")
    old = _old_file(tmp_path)
    mw = FileTrackerMiddleware(enforce_read_before_edit=False, workspace_root=str(tmp_path))
    assert mw._check_edit_allowed(_write("/fresh.py")) is None
    get_session_tracker().record_write("/old.py", "x", operation="write")
    assert mw._check_edit_allowed(_write(str(old))) is None
    reset_session_tracker()
    assert FileTrackerMiddleware()._check_edit_allowed(_write(str(old))) is None

if __name__ == "__main__":
    test_edit_on_unseen_file_is_rejected_as_error()
    test_edit_after_read_is_allowed()
    test_edit_after_write_is_allowed()
    test_gate_disabled_allows_everything()
    print("ALL TESTS PASSED")
