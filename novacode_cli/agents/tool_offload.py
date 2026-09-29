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
        before = {
            msg.tool_call_id: (msg.name, msg.content)
            for msg in messages
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
            name, payload = before.get(msg.tool_call_id, (None, None))
            # A failed save still leaves the result cleared — the context
            # pressure is real either way — but then it must say so, not leave
            # the bare marker for the model to interpret.
            saved = self._save(msg.tool_call_id, name, payload) or UNSAVED
            messages[idx] = msg.model_copy(update={"content": saved})

    def _save(self, call_id: str, name: str | None, payload: object) -> str | None:
        """Write *payload* out; return the replacement placeholder, or None."""
        text = payload if isinstance(payload, str) else str(payload or "")
        if not text.strip() or self.offload_dir is None:
            return None
        stem = _SAFE.sub("-", f"{name or 'tool'}-{call_id}")[:80]
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
        # them in place, and the same marker identifies what it cleared.
        before = {
            msg.tool_call_id: (msg.name, msg.content)
            for msg in messages
            if isinstance(msg, ToolMessage)
        }
        judged = self._judged_stale_ids(messages)
        super().apply(messages, count_tokens=count_tokens)
        self._restore_unjudged(messages, before, judged)

    def _judged_stale_ids(self, messages: list[AnyMessage]) -> set[str]:
        """Ids of results the model scored stale and this edit may clear."""
        cleared = {
            message.tool_call_id
            for message in messages
            if isinstance(message, ToolMessage)
            and message.response_metadata.get("context_editing", {}).get("cleared")
        }
        # The base class clears everything but the last `keep` results (and only
        # when the pool exceeds `keep` at all), before any verdict is consulted.
        # A restored result keeps unmarked metadata, so the *position* of a
        # result — not its metadata — is the record of past clearing: a result
        # that sits inside the retained tail must never be re-cleared later.
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if self.keep >= len(results):
            return set()
        window = results[: len(results) - self.keep]
        excluded = set(self.exclude_tools)

        judged: set[str] = set()
        for message in window:
            if message.tool_call_id in cleared:
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
                judged.add(message.tool_call_id)
        return judged

    def _restore_unjudged(
        self,
        messages: list[AnyMessage],
        before: dict[str, tuple[str | None, object]],
        judged: set[str],
    ) -> None:
        """Put back every result the base class touched without a stale verdict.

        The base class decides what to touch; this method is the veto. A result
        is restored byte-for-byte, so a message the model never judged is
        indistinguishable from one it scored as still needed.

        The marker is *identity* -- ``OffloadingToolUsesEdit`` replaces the
        message with a copy carrying a fresh offload note, so two results share
        the placeholder only before the base class runs. It only ever touches
        results that carry a matching tool call in an earlier ``AIMessage``, and
        restoring is a no-op when the bytes are already right.
        """
        for index, msg in enumerate(messages):
            if not isinstance(msg, ToolMessage):
                continue
            if msg.tool_call_id in judged:
                continue
            name, payload = before.get(msg.tool_call_id, (None, None))
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
