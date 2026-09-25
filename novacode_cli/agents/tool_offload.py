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


__all__ = ["MAX_AGE_SECONDS", "UNSAVED", "VIRTUAL_PREFIX", "OffloadingToolUsesEdit", "cleared_dir"]
