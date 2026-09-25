"""One shared policy for reacting to context pressure.

Every renderer must apply the SAME rule. It previously lived inline in
``tui/app.py`` only, so the console path recorded the breakdown and then did
nothing — no warning, no compaction, straight into the provider's limit on a
long session.

Kept pure (no agent, no model, no I/O) so the policy is unit-testable without a
running agent and cannot drift between surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from novacode_cli.context._analysis import (
    CONTEXT_CRITICAL_THRESHOLD,
    CONTEXT_WARNING_THRESHOLD,
    MIN_RESERVE_TOKENS,
    compact_threshold_pct,
)


class PressureAction(Enum):
    """What the caller should do about current context usage."""

    NONE = "none"
    """Comfortably below the warning threshold — do nothing."""

    WARN = "warn"
    """Past the warning threshold — tell the user, suggest /compact."""

    COMPACT = "compact"
    """Past the auto-compaction threshold — compact now."""


@dataclass(frozen=True)
class PressureDecision:
    """The decision plus the state the caller must carry forward."""

    action: PressureAction
    reason: str
    usage_percentage: float
    compacted_last_turn: bool
    """Value the caller should store as its `_compacted_last_turn` flag."""

    disable_auto_compact: bool = False
    """Set once repeated compaction demonstrably is not freeing space.

    A too-small window, or an agent that keeps re-filling context (e.g. by
    re-reading the offloaded history to "recover" the task), makes compaction a
    no-op — repeating it only spams the summary. The caller stops and hands
    control back to the user instead.
    """


def assess_pressure(
    usage_percentage: float,
    *,
    compacted_last_turn: bool = False,
    auto_compact_enabled: bool = True,
    context_window: int = 0,
) -> PressureDecision:
    """Decide how to react to ``usage_percentage`` of the model's window.

    Args:
        usage_percentage: Current usage as a percentage (0-100+).
        compacted_last_turn: Whether the caller already auto-compacted on the
            immediately preceding turn. If usage is still critical after that,
            compaction is not winning and the loop is broken.
        auto_compact_enabled: False when the caller has already disabled
            auto-compaction for this session; it then only warns.
        context_window: The model's window in tokens. Given, the compaction
            point also has to leave :data:`MIN_RESERVE_TOKENS` free — see
            :func:`compact_threshold_pct`. 0 keeps the plain percentage.

    Returns:
        A :class:`PressureDecision`. ``action`` is ``COMPACT`` only when
        auto-compaction is enabled AND it has not already failed to help.
    """
    if usage_percentage >= compact_threshold_pct(context_window):
        if not auto_compact_enabled:
            return PressureDecision(
                action=PressureAction.WARN,
                reason=(
                    f"Context critical: {usage_percentage:.0f}% — auto-compact is disabled; run "
                    "/compact now, or /clear to start fresh."
                ),
                usage_percentage=usage_percentage,
                compacted_last_turn=False,
            )
        if compacted_last_turn:
            # Compaction ran last turn and we are critical again → not helping.
            return PressureDecision(
                action=PressureAction.WARN,
                reason=(
                    "Repeated auto-compaction isn't freeing space (likely a "
                    "context-window loop). Auto-compact disabled — use /clear to "
                    "start fresh, or switch to a larger-context model."
                ),
                usage_percentage=usage_percentage,
                compacted_last_turn=False,
                disable_auto_compact=True,
            )
        return PressureDecision(
            action=PressureAction.COMPACT,
            reason=f"Context {usage_percentage:.0f}% — auto-compacting to free space…",
            usage_percentage=usage_percentage,
            compacted_last_turn=True,
        )

    if usage_percentage >= CONTEXT_WARNING_THRESHOLD * 100:
        return PressureDecision(
            action=PressureAction.WARN,
            reason=f"Context usage high: {usage_percentage:.0f}% — consider /compact soon.",
            usage_percentage=usage_percentage,
            # A compaction (if any) did free space — reset the loop guard.
            compacted_last_turn=False,
        )

    return PressureDecision(
        action=PressureAction.NONE,
        reason="",
        usage_percentage=usage_percentage,
        compacted_last_turn=False,
    )


def post_compaction_still_critical(
    usage_percentage: float, *, auto_compact_enabled: bool, context_window: int = 0
) -> PressureDecision:
    """Re-assess right after a compaction, to detect that it is not working.

    Returns ``disable_auto_compact=True`` when usage is still at the compaction
    threshold — the summary itself is near the window (usually a too-small
    model), so further auto-compaction is futile.
    """
    decision = assess_pressure(
        usage_percentage,
        compacted_last_turn=False,
        auto_compact_enabled=auto_compact_enabled,
        context_window=context_window,
    )
    if decision.action is PressureAction.COMPACT:
        return PressureDecision(
            action=PressureAction.WARN,
            reason=(
                "Auto-compact couldn't free enough space (context window too small "
                "for this conversation). Auto-compact disabled — use /clear to start "
                "fresh or switch to a larger-context model."
            ),
            usage_percentage=usage_percentage,
            compacted_last_turn=False,
            disable_auto_compact=True,
        )
    return decision


__all__ = [
    "CONTEXT_CRITICAL_THRESHOLD",
    "MIN_RESERVE_TOKENS",
    "PressureAction",
    "PressureDecision",
    "assess_pressure",
    "compact_threshold_pct",
    "post_compaction_still_critical",
]
