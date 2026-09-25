"""``ClearToolUsesEdit.trigger`` must scale with the model's window.

The library takes an absolute token count, so a fixed value is
window-independent: it never fired on the small Ollama windows in
``MODEL_CONTEXT_WINDOWS`` (~40K) while clearing needlessly early on a 200K
model. These pin the resolution from a window to that absolute trigger, and
the ordering invariant against Nova's own compaction threshold.
"""

from __future__ import annotations

from novacode_cli.agents.core_agent import _context_edit_trigger
from novacode_cli.context import (
    AUTO_COMPACT_THRESHOLD,
    CONTEXT_EDIT_TRIGGER_FRACTION,
)


def test_trigger_scales_with_window() -> None:
    assert _context_edit_trigger(200_000) == int(200_000 * CONTEXT_EDIT_TRIGGER_FRACTION)
    assert _context_edit_trigger(40_960) == int(40_960 * CONTEXT_EDIT_TRIGGER_FRACTION)


def test_small_window_now_fires_at_all() -> None:
    """The regression this fixes: a 40K window must produce a trigger BELOW
    its window, or the reducer can never fire."""
    trigger = _context_edit_trigger(40_960)
    assert trigger < 40_960


def test_unknown_window_falls_back_to_legacy_fixed_count() -> None:
    assert _context_edit_trigger(0) == 60_000
    assert _context_edit_trigger(-1) == 60_000


def test_trigger_has_a_floor_for_tiny_windows() -> None:
    """A floor, but never above the point where compaction would take over."""
    from novacode_cli.context import compact_threshold_pct

    assert _context_edit_trigger(1_000) == 250, "a floor, but relative to the window"
    for window in (1_000, 8_192, 40_960, 200_000):
        assert _context_edit_trigger(window) >= window * 0.25, window
        assert _context_edit_trigger(window) < window * compact_threshold_pct(window) / 100


def test_clearing_precedes_whole_history_compaction() -> None:
    """Cheap tool-result clearing must get its chance before compaction."""
    assert CONTEXT_EDIT_TRIGGER_FRACTION < AUTO_COMPACT_THRESHOLD
