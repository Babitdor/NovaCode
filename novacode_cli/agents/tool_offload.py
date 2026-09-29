"""Restorable clearing of old tool results.

``ClearToolUsesEdit`` replaces an old tool result with a fixed placeholder that
tells the model to run the tool again. That is only true for a *repeatable*
tool: a 4-minute test run, a shell command with side effects, a web search or a
one-shot MCP call cannot be re-run to get the same bytes back, so the
information is simply gone.

Manus calls the safe version **restorable compaction**: strip from the context
only what an external store can reconstruct — drop a page's body, keep its URL;
drop a file's content, keep its path. Claude Code does the same thing under the
name microcompaction (large tool outputs go to disk, a path stays inline).

This module is that disk tier. The payload is written to the agent's ``cleared``
directory, which is mounted read-only-in-practice at :data:`VIRTUAL_PREFIX`, and
the placeholder names the file so ``read_file`` can bring it back.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents.middleware import ClearToolUsesEdit
from langchain_core.messages import AnyMessage, ToolMessage

logger = logging.getLogger(__name__)

#: Virtual path the offload directory is mounted at (a CompositeBackend route).
VIRTUAL_PREFIX = "/cleared/"

#: Offloaded payloads older than this are deleted on the next offload. They only
#: matter to the conversation that produced them, and an unbounded directory of
#: tool output is its own bug.
MAX_AGE_SECONDS = 7 * 24 * 3600

#: Shown when the payload could not be written (disk full, read-only home): the
#: old behaviour, which is honest about what was lost.
UNSAVED = "[Old tool result cleared to save context. Re-run the tool if you need it again.]"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_pruned = False


def _offload_stem(name: str | None, call_id: str, text: str) -> str:
    """Filename stem for an offloaded payload. The digest is the uniqueness claim.

    ``name`` + ``call_id`` is not an identity: a resumed session reuses
    ``tool_call_id`` values -- one archive had 444 results under 122 distinct ids
    (see :func:`novacode_cli.agents.tool_verdicts.result_key`) -- so two results
    with the same id would name the same file and the second would silently
    overwrite the first, while the first result's placeholder still points at
    it. Recovery would then hand back the wrong bytes with no sign anything went
    wrong. Hashing the payload into the name gives distinct bytes distinct
    files; identical bytes sharing one file is harmless.

    The digest is appended *after* truncation, so a long tool name or id can
    never cut it off -- which would put the collision straight back.
    """
    prefix = _SAFE.sub("-", f"{name or 'tool'}-{call_id}")[:64]
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def cleared_dir(agent_dir: Path) -> Path:
    """Directory backing :data:`VIRTUAL_PREFIX` for *agent_dir*."""
    return agent_dir / "cleared"


def _prune(directory: Path) -> None:
    """Delete offloads older than :data:`MAX_AGE_SECONDS`. Once per process."""
    global _pruned
    if _pruned:
        return
    _pruned = True
    cutoff = time.time() - MAX_AGE_SECONDS
    try:
        for path in directory.glob("*.txt"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError:  # housekeeping never blocks the turn
        logger.debug("Could not prune %s", directory, exc_info=True)


# No slots=True: it rebuilds the class, which breaks the zero-arg `super()`
# below (the __class__ cell still points at the pre-slots class).
@dataclass
class OffloadingToolUsesEdit(ClearToolUsesEdit):
    """``ClearToolUsesEdit`` that writes the payload out instead of dropping it."""

    offload_dir: Path | None = None

    def apply(self, messages: list[AnyMessage], *, count_tokens) -> None:  # noqa: ANN001
        """Clear old tool results, keeping their payloads on disk."""
        # The payloads have to be captured BEFORE the base class overwrites
        # them; it rewrites `messages[idx]` in place with a copy carrying the
        # placeholder, so afterwards the original content is unreachable.
        # Keyed by position rather than by tool_call_id, which is NOT unique:
        # a resumed session reuses ids, and an id-keyed map would keep only the
        # last payload for that id and hand it back for every result sharing it.
        before = {
            idx: (msg.name, msg.content)
            for idx, msg in enumerate(messages)
            if isinstance(msg, ToolMessage)
        }
        super().apply(messages, count_tokens=count_tokens)
        if self.offload_dir is None:
            return
        for idx, msg in enumerate(messages):
            # The base class sets content to exactly `self.placeholder` on the
            # messages it just cleared — that is the marker for "this one".
            if not isinstance(msg, ToolMessage) or msg.content != self.placeholder:
                continue
            name, payload = before.get(idx, (None, None))
            # A failed save still leaves the result cleared — the context
            # pressure is real either way — but then it must say so, not leave
            # the bare marker for the model to interpret.
            saved = self._save(msg.tool_call_id, name, payload) or UNSAVED
            messages[idx] = msg.model_copy(update={"content": saved})

    def _save(self, call_id: str, name: str | None, payload: object) -> str | None:
        """Write *payload* out; return the replacement placeholder, or None.

        The placeholder names the file so ``read_file`` can bring the payload
        back, so the name has to be unique per payload -- see
        :func:`_offload_stem`.
        """
        text = payload if isinstance(payload, str) else str(payload or "")
        if not text.strip() or self.offload_dir is None:
            return None
        stem = _offload_stem(name, call_id, text)
        path = self.offload_dir / f"{stem}.txt"
        try:
            self.offload_dir.mkdir(parents=True, exist_ok=True)
            _prune(self.offload_dir)
            path.write_text(text, encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("Could not offload tool result %s", call_id, exc_info=True)
            return None
        return (
            f"[Old {name or 'tool'} result moved out of context to save space. "
            f"The full output is saved at {VIRTUAL_PREFIX}{path.name} — "
            f"read_file it if you need it again.]"
        )


# No slots=True, for the same reason as the base class above.
@dataclass
class VerdictToolUsesEdit(OffloadingToolUsesEdit):
    """Clear only the results a decision model scored stale.

    Identical to :class:`OffloadingToolUsesEdit` in every other respect — same
    offload, same recovery, same placeholder — but the *selection* is not "every
    result older than the trigger except the last few". A result is cleared only
    when its cached verdict says it is no longer needed:

    * no verdict (never scored, or scored under a different value) → **keep**.
      Failing open is what makes a cold cache, a slow model or an unreachable
      endpoint safe: the history is only ever *less* pruned than the heuristic,
      never more than the model has actually judged.
    * verdict at or above ``keep_threshold`` → keep.

    The ``trigger`` still gates the whole edit, so nothing is cleared before the
    same context pressure the heuristic waits for, and the newest ``keep``
    results stay verbatim either way. Verdicts arrive from
    :mod:`novacode_cli.agents.tool_verdicts`; this class only reads them, which
    keeps the hot path (``apply`` runs on every model call) free of I/O.
    """

    verdicts: Any = None
    keep_threshold: float = 0.5

    def apply(self, messages: list[AnyMessage], *, count_tokens) -> None:  # noqa: ANN001
        """Clear only judged-stale results; capture payloads before they vanish."""
        if self.verdicts is None:
            return
        # As in the base class: the payloads must be read before super() rewrites
        # them in place, and the same marker identifies what it cleared. Keyed by
        # position, because tool_call_id is not unique across a resumed session
        # and an id-keyed map would restore the same payload for every result
        # sharing that id.
        before = {
            idx: (msg.name, msg.content)
            for idx, msg in enumerate(messages)
            if isinstance(msg, ToolMessage)
        }
        judged = self._judged_stale_ids(messages)
        super().apply(messages, count_tokens=count_tokens)
        self._restore_unjudged(messages, before, judged)

    def _judged_stale_ids(self, messages: list[AnyMessage]) -> set[int]:
        """Positions of the results the model scored stale and this edit may clear.

        Positions, not ``tool_call_id`` values: the id is not unique across a resumed
        session, so an id-keyed set would mark every result sharing an id as
        judged once any one of them scored stale, and the others would be cleared
        without ever being judged.
        """
        cleared = {
            index
            for index, message in enumerate(messages)
            if isinstance(message, ToolMessage)
            and message.response_metadata.get("context_editing", {}).get("cleared")
        }
        # The base class clears everything but the last `keep` results (and only
        # when the pool exceeds `keep` at all), before any verdict is consulted.
        #
        # `cleared` marks a result the base class has already reached. It is not
        # unset by _restore_unjudged -- that only rewrites content and name -- so
        # a result restored on an earlier pass still carries it, and is skipped
        # here. Two consequences worth knowing: a result judged stale is never
        # re-judged, and a result restored as "no verdict" is never re-judged
        # either, so it stays verbatim even once a verdict exists for it.
        results = [
            (index, message)
            for index, message in enumerate(messages)
            if isinstance(message, ToolMessage)
        ]
        if self.keep >= len(results):
            return set()
        window = results[: len(results) - self.keep]
        excluded = set(self.exclude_tools)

        judged: set[int] = set()
        for index, message in window:
            if index in cleared:
                continue
            if (message.name or "") in excluded:
                continue
            try:
                stale = self.verdicts.stale(
                    message, keep_threshold=self.keep_threshold
                )
            except Exception:  # noqa: BLE001 — an unreadable cache must never clear
                logger.debug("Could not read a verdict", exc_info=True)
                stale = None
            if stale:
                judged.add(index)
        return judged

    def _restore_unjudged(
        self,
        messages: list[AnyMessage],
        before: dict[int, tuple[str | None, object]],
        judged: set[int],
    ) -> None:
        """Put back every result the base class touched without a stale verdict.

        The base class decides what to touch; this method is the veto. A result
        is restored byte-for-byte, so a message the model never judged is
        indistinguishable from one it scored as still needed.

        Identity here is *position*, not ``tool_call_id``: the base class rewrites
        ``messages[idx]`` in place, so the index is the only handle that survives
        its edit, and the id is not unique across a resumed session. Restoring is
        a no-op when the bytes are already right.
        """
        for index, msg in enumerate(messages):
            if not isinstance(msg, ToolMessage):
                continue
            if index in judged:
                continue
            name, payload = before.get(index, (None, None))
            content = payload if isinstance(payload, str) else str(payload or "")
            if msg.content == content and msg.name == name:
                continue
            messages[index] = msg.model_copy(
                update={
                    "content": content,
                    "name": name if name is not None else msg.name,
                }
            )


__all__ = [
    "MAX_AGE_SECONDS",
    "UNSAVED",
    "VIRTUAL_PREFIX",
    "OffloadingToolUsesEdit",
    "VerdictToolUsesEdit",
    "cleared_dir",
]
