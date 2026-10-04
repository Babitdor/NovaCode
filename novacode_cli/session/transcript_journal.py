"""What a session's transcript showed that its message history does not hold.

A saved session is the agent's messages: prompts, replies, tool calls. Things
the *user* ran in the UI never enter that list, so a resumed session used to
come back without them:

* ``!`` shell commands and their output;
* background jobs started with ctrl+b (shell and agent).

This journal records those, appended as they finish, in
``<sessions_dir>/<session_id>/ui_journal.jsonl``. It also records a marker for
every prompt the user sent, which is the only thing that lets the entries be put
back in the right place: on resume the markers are matched to the replayed
human messages, and each entry is shown after the turn it followed.

Entries (one JSON object per line, ``k`` is the kind):

    {"k": "user", "text": "..."}                       a prompt marker
    {"k": "bash", "cmd": "...", "lines": [...], "exit": 0, "fg": true}
    {"k": "bgagent", "prompt": "...", "status": "done", "summary": "..."}

Writes go through one worker thread: they stay off the UI loop and stay in order.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILE_NAME = "ui_journal.jsonl"

#: Output lines kept per command. The transcript shows 22 rows; this keeps
#: enough to scroll back through without storing a build log per command.
MAX_LINES = 200
_MAX_LINE_CHARS = 2_000

#: Entries read back on resume. The transcript itself is capped at 200 widgets.
MAX_ENTRIES = 400

_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nova-journal")


def path_for(sessions_dir: Path, session_id: str) -> Path:
    return Path(sessions_dir) / session_id / FILE_NAME


def _append(path: Path, entry: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — the journal must never break a session
        logger.debug("journal append failed", exc_info=True)


def append(sessions_dir: Path, session_id: str, entry: dict[str, Any]) -> None:
    """Queue *entry* for the session's journal. Returns at once."""
    if entry.get("k") == "bash":
        entry = {**entry, "lines": [ln[:_MAX_LINE_CHARS] for ln in entry.get("lines", [])][-MAX_LINES:]}
    _writer.submit(_append, path_for(sessions_dir, session_id), entry)


def flush() -> None:
    """Wait for queued writes (tests, and a read that must see its own writes)."""
    _writer.submit(lambda: None).result()


def load(sessions_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """The session's journal, oldest first; ``[]`` if there is none."""
    path = path_for(sessions_dir, session_id)
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line from a crash: skip it
                if isinstance(entry, dict) and "k" in entry:
                    out.append(entry)
    except OSError:
        return []
    return out[-MAX_ENTRIES:]


def place(entries: list[dict[str, Any]], human_texts: list[str]) -> tuple[list[list[dict]], list[dict]]:
    """Assign journal entries to the replayed human messages.

    Returns ``(per_turn, leading)``: ``per_turn[i]`` are the entries to show
    after the turn that *human_texts[i]* opened, and ``leading`` are entries
    that came before the first matched prompt and belong above everything.

    Markers are matched to messages from the newest backwards, by text, because
    the two lists drift apart at the old end: the replay shows only a window of
    the history, and compaction rewrites the oldest messages away. An entry
    whose turn is no longer replayed is dropped, like the turn itself. With no
    human message at all (a session that only ran commands), everything is
    ``leading``.
    """
    items = [e for e in entries if e.get("k") != "user"]
    per_turn: list[list[dict]] = [[] for _ in human_texts]
    if not human_texts:
        return per_turn, items

    marker_at = [i for i, e in enumerate(entries) if e.get("k") == "user"]
    matched: dict[int, int] = {}  # human index -> position in `entries`
    m = len(marker_at) - 1
    for h in range(len(human_texts) - 1, -1, -1):
        want = human_texts[h].strip()
        k = m
        while k >= 0 and (entries[marker_at[k]].get("text") or "").strip() != want:
            k -= 1
        if k >= 0:
            matched[h] = marker_at[k]
            m = k - 1
    if not matched:
        # Nothing lines up (e.g. the journal predates these messages): show the
        # entries at the end rather than lose them.
        per_turn[-1] = items
        return per_turn, []

    order = sorted(matched)  # human indices, oldest first
    leading: list[dict] = []
    first = matched[order[0]]
    if first == marker_at[0]:  # nothing older was dropped, so these are not orphans
        leading = [e for e in entries[:first] if e.get("k") != "user"]
    for n, h in enumerate(order):
        start = matched[h]
        end = matched[order[n + 1]] if n + 1 < len(order) else len(entries)
        per_turn[h] = [e for e in entries[start:end] if e.get("k") != "user"]
    return per_turn, leading


if __name__ == "__main__":
    # ponytail: self-check — entries land after the turn they followed, old
    # ones fall off with their turn, and a commands-only session keeps them.
    u = lambda t: {"k": "user", "text": t}  # noqa: E731
    b = lambda c: {"k": "bash", "cmd": c}  # noqa: E731
    log = [b("pre"), u("one"), b("a"), u("two"), u("three"), b("c"), b("d")]
    per, lead = place(log, ["one", "two", "three"])
    assert lead == [b("pre")] and per == [[b("a")], [], [b("c"), b("d")]], (per, lead)
    per, lead = place(log, ["two", "three"])  # "one" scrolled out of the window
    assert lead == [] and per == [[], [b("c"), b("d")]], (per, lead)
    per, lead = place([b("x")], [])
    assert lead == [b("x")] and per == []
    per, lead = place([b("x")], ["never journalled"])
    assert per == [[b("x")]] and lead == []
    print("ok")
