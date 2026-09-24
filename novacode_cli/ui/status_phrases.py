"""Rotating status phrases for Nova's live status line.

Every UI (Rich console, Textual TUI, remote bridges) shows a one-line "what is
Nova doing" status. A single fixed string ("Nova is thinking…") gets stale fast
on a long turn, so each *phase* draws from a pool of interchangeable phrases
instead.

Two access patterns are provided, and the distinction matters:

``rotating(category)``
    Advances to the next phrase on every call. Use it for one-shot sites — the
    start of a turn, a subagent dispatch — where a fresh phrase each time is the
    point.

``sticky(category)`` / ``reset(categories)``
    Returns the same phrase until explicitly reset. Use it on hot paths. The TUI
    streams tokens and updates its status line on every one; it guards the
    rebuild with ``if self._activity != <phrase>``. A phrase that re-rolled per
    call would defeat that guard and rebuild the status line once per token,
    which is a measurable regression, not a cosmetic one.

Rotation is deterministic (a per-category cursor, not ``random``) so the
sequence is reproducible in tests and every phrase is eventually shown.
"""

from __future__ import annotations

#: Phrase pools, keyed by category. Each entry completes "{agent} ___", so the
#: leading verb and trailing ellipsis are part of the phrase.
_POOLS: dict[str, tuple[str, ...]] = {
    "thinking": (
        "is dilly dallying…",
        "is noodling on it…",
        "is mulling this over…",
        "is chewing on the problem…",
        "is connecting the dots…",
        "is rummaging through the toolbox…",
        "is turning it over…",
        "is having a good long think…",
        "is sharpening a pencil…",
        "is consulting the rubber duck…",
        "is pacing the workshop…",
        "is staring thoughtfully into the middle distance…",
    ),
    "responding": (
        "is putting pen to paper…",
        "is finding the right words…",
        "is writing this up…",
        "is drafting a reply…",
        "is lining up the sentences…",
        "is tapping out an answer…",
        "is getting the phrasing right…",
    ),
    "synthesizing": (
        "is pulling the threads together…",
        "is stitching it all up…",
        "is reconciling the findings…",
        "is tidying the loose ends…",
        "is drawing the conclusions…",
    ),
    "subagent": (
        "is on the case…",
        "is digging in…",
        "is off investigating…",
        "is doing the legwork…",
        "is chasing that down…",
        "is combing through it…",
    ),
    "working": (
        "is rolling up the sleeves…",
        "is getting to work…",
        "is on it…",
    ),
}

#: Per-category rotation cursor for :func:`rotating`.
_cursor: dict[str, int] = {}

#: Per-category phrase held by :func:`sticky` until :func:`reset`.
_stuck: dict[str, str] = {}


def _pool(category: str) -> tuple[str, ...]:
    """Return the pool for *category*, falling back to ``thinking``.

    An unknown category is a programming error, not a user-facing condition, so
    it degrades to the most generic pool rather than raising into a UI path.

    Args:
        category: Pool name (see :data:`_POOLS`).

    Returns:
        The phrase tuple for the category.
    """
    return _POOLS.get(category) or _POOLS["thinking"]


def rotating(category: str) -> str:
    """Return the next phrase for *category*, advancing the rotation.

    Args:
        category: Pool name (see :data:`_POOLS`).

    Returns:
        A phrase such as ``"is dilly dallying…"`` (no agent name).
    """
    pool = _pool(category)
    idx = _cursor.get(category, 0)
    _cursor[category] = idx + 1
    return pool[idx % len(pool)]


def sticky(category: str) -> str:
    """Return a phrase for *category* that does not change until reset.

    Args:
        category: Pool name (see :data:`_POOLS`).

    Returns:
        The phrase chosen for this category, stable across calls.
    """
    phrase = _stuck.get(category)
    if phrase is None:
        phrase = rotating(category)
        _stuck[category] = phrase
    return phrase


def reset(categories: str | None = None) -> None:
    """Drop the held phrase(s) so the next :func:`sticky` picks a new one.

    ``rotating`` already advances on its own; this affects ``sticky`` only.
    Call it at a phase boundary (typically the start of a turn) so a new turn
    gets a new phrase while the turn itself stays stable.

    Args:
        categories: A category to release. ``None`` releases all of them.
    """
    if categories is None:
        _stuck.clear()
    else:
        _stuck.pop(categories, None)


def status_line(category: str, agent_name: str = "Nova", *, sticky_phrase: bool = False) -> str:
    """Build a full ``"{agent} {phrase}"`` status string.

    Args:
        category: Pool name (see :data:`_POOLS`).
        agent_name: Name to prefix, e.g. ``"Nova"`` or a subagent type.
        sticky_phrase: When True use :func:`sticky` instead of :func:`rotating`.

    Returns:
        A display string such as ``"Nova is dilly dallying…"``.
    """
    phrase = sticky(category) if sticky_phrase else rotating(category)
    return f"{agent_name} {phrase}"
