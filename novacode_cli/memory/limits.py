"""Shared limits for the memory tiers.

Single source of truth for the per-file character budget used on both the
**write** side (Semantic-tier compaction in ``hermes/memory_tiers.py``) and the
**read** side (prompt-injection truncation in ``memory/agent_memory.py``). These
were previously two independent ``MAX_MEMORY_CHARS = 12_000`` constants that
could silently drift.

Compaction invariant (keep-newest):
    Memory files are written newest-first (new sections/lessons are prepended,
    above older ones). Both compaction and injection-time truncation therefore
    keep the **head** of the file — which is the most recent content. Any new
    memory writer MUST prepend, not append, to preserve this.
"""

from __future__ import annotations

# Maximum characters per memory file before disk compaction / truncation
# (~3,000 tokens at 4 chars/token). This is the WRITE-side cap only: it bounds
# what survives on disk, and is deliberately larger than the injection budget
# below so raising the budget later can still recover content.
MAX_MEMORY_CHARS = 12_000


#: Share of the context window ONE injected memory block may occupy. Four
#: blocks are injected every turn (agent.md, INDEX.md, HABITS.md, project
#: memory), so the tier as a whole is capped at ~12% of the window.
MEMORY_WINDOW_FRACTION = 0.03


#: Default per-block *injection* budget (agent.md, INDEX, HABITS, project
#: memory). Lowered from the 12k disk cap: the injected head is what the model
#: reads every turn, and older content is reachable on demand via
#: ``memory_search``. User-overridable in Nova.config.json
#: (``memory_block_chars``).
DEFAULT_MEMORY_BLOCK_CHARS = 6_000


#: Chars of the topic-memory INDEX injected into the system prompt. The index is
#: a pointer list (newest first); the rest is reachable via ``memory_search``, so
#: a small slice is enough to orient without paying for ~100k chars of pointers.
DEFAULT_MEMORY_INDEX_CHARS = 2_000


def memory_budget(context_window: int, cap: int = DEFAULT_MEMORY_BLOCK_CHARS) -> int:
    """Chars one injected memory block may use, given the model's window.

    ``MAX_MEMORY_CHARS`` is a *disk* cap and is window-independent — but as an
    injection cap it was the same 12k on a 200K Claude window and on a 40K
    Ollama one, where the four blocks together could claim a third of the
    window before the conversation started. Skills already budget this way
    (``skills.refreshing_middleware.listing_budget``); memory now does too.

    ``cap`` overrides the ceiling (the user-configurable per-block budget);
    the window fraction still applies, so a small window is never overrun.
    """
    if context_window <= 0:
        return cap
    return max(2_000, min(cap, int(context_window * MEMORY_WINDOW_FRACTION * 4)))
