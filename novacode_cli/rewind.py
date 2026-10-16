"""Code checkpoints for /rewind: each file as it was before the agent changed it.

Before every ``write_file`` / ``edit_file`` the file's bytes (or the fact that
it did not exist yet) are kept under ``~/.nova/rewind/<thread_id>/``, keyed by
the tool call. ``/rewind`` to a message restores every file changed after it
to its state before the first such change, and deletes the files created since.

ponytail: file tools only. A file changed by a shell command (``sed -i``, a
build) is not checkpointed; git is the safety net there.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

ROOT = Path.home() / ".nova" / "rewind"
_MAX_BYTES = 10 * 1024 * 1024  # larger files are not checkpointed
_lock = threading.Lock()


def _dir(thread_id: str) -> Path:
    return ROOT / thread_id


def physical_path(path: str, workspace_root: Path) -> Path | None:
    """Where a file tool's ``path`` is on disk; None outside the workspace.

    The file tools take workspace-virtual paths (``/src/app.py``); an absolute
    path into the workspace works too. Agent memory and other virtual routes
    fall outside the workspace and are not checkpointed.
    """
    if not path:
        return None
    root = workspace_root.resolve()
    candidate = Path(path)
    if not candidate.drive and not str(candidate).startswith(str(root)):
        candidate = root / path.lstrip("/\\")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def record(thread_id: str, call_id: str, path: Path) -> None:
    """Keep ``path`` as it is now, before tool call ``call_id`` changes it."""
    if not thread_id or not call_id:
        return
    try:
        existed = path.is_file()
        if existed and path.stat().st_size > _MAX_BYTES:
            return
        data = path.read_bytes() if existed else None
    except OSError:
        return
    folder = _dir(thread_id)
    with _lock:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            index = folder / "index.jsonl"
            blob = None
            if data is not None:
                blob = f"{uuid.uuid4().hex}.bin"
                (folder / blob).write_bytes(data)
            with open(index, "a", encoding="utf-8") as f:
                f.write(json.dumps({"call": call_id, "path": str(path), "blob": blob}) + "\n")
        except OSError:
            pass


def _records(thread_id: str) -> list[dict]:
    try:
        lines = (_dir(thread_id) / "index.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def changes_after(thread_id: str, call_ids: set[str]) -> dict[str, str | None]:
    """Each file changed from the first of ``call_ids`` on: path -> blob (None: did not exist).

    Everything recorded from that point is included, so a subagent's edits
    made after it count too. The first record of a path is its state then.
    """
    records = _records(thread_id)
    start = next((i for i, r in enumerate(records) if r["call"] in call_ids), None)
    if start is None:
        return {}
    before: dict[str, str | None] = {}
    for item in records[start:]:
        before.setdefault(item["path"], item["blob"])
    return before


def restore(thread_id: str, before: dict[str, str | None]) -> list[str]:
    """Put each file back as it was; delete the ones that did not exist. Returns the paths."""
    restored = []
    for path, blob in before.items():
        target = Path(path)
        try:
            if blob is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((_dir(thread_id) / blob).read_bytes())
            restored.append(path)
        except OSError:
            continue
    return restored


def user_turns(messages: list) -> list[tuple[int, str]]:
    """``(index, text)`` of each message the user typed, oldest first.

    Nova's own human-role messages (skill hints, summaries, imported context)
    are not places to rewind to.
    """
    from novacode_cli.compaction import is_compaction_summary
    from novacode_cli.core.streaming import is_internal_context_text
    from novacode_cli.session.imported_context import MESSAGE_ID

    turns = []
    for index, message in enumerate(messages):
        if getattr(message, "type", "") != "human" or getattr(message, "id", None) == MESSAGE_ID:
            continue
        if (getattr(message, "additional_kwargs", None) or {}).get("lc_source"):
            continue  # summaries, pinned skills
        content = message.content
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
        text = text.strip()
        if text and not is_compaction_summary(text) and not is_internal_context_text(text) \
                and not text.startswith("Internal context:"):
            turns.append((index, text))
    return turns


def call_ids(messages: list) -> set[str]:
    """The tool call ids the assistant made in ``messages``."""
    return {
        str(call.get("id"))
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
        if call.get("id")
    }
