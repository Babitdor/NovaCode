"""One shared context-pressure policy for every UI surface.

The rule previously lived inline in ``tui/app.py`` only, so the Rich console
REPL recorded the breakdown and then did nothing — no warning, no compaction,
straight into the provider's limit on a long session. These pin the policy so
both surfaces necessarily agree.
"""

from __future__ import annotations

from novacode_cli.context import (
    AUTO_COMPACT_THRESHOLD,
    CONTEXT_CRITICAL_THRESHOLD,
    CONTEXT_WARNING_THRESHOLD,
    PressureAction,
    assess_pressure,
    post_compaction_still_critical,
)

_CRITICAL_PCT = AUTO_COMPACT_THRESHOLD * 100


def test_below_warning_does_nothing() -> None:
    d = assess_pressure(10.0)
    assert d.action is PressureAction.NONE
    assert not d.disable_auto_compact


def test_between_warning_and_compact_warns_only() -> None:
    mid = (CONTEXT_WARNING_THRESHOLD * 100 + AUTO_COMPACT_THRESHOLD * 100) / 2
    d = assess_pressure(mid)
    assert d.action is PressureAction.WARN
    assert "/compact" in d.reason


def test_at_compaction_threshold_compacts() -> None:
    d = assess_pressure(_CRITICAL_PCT)
    assert d.action is PressureAction.COMPACT
    assert d.compacted_last_turn is True


def test_compacting_twice_in_a_row_disables_auto_compact() -> None:
    """Compaction ran last turn and we are critical again → it isn't winning."""
    d = assess_pressure(_CRITICAL_PCT + 1, compacted_last_turn=True)
    assert d.action is PressureAction.WARN
    assert d.disable_auto_compact is True
    assert "/clear" in d.reason


def test_disabled_auto_compact_never_requests_compaction() -> None:
    d = assess_pressure(_CRITICAL_PCT + 10, auto_compact_enabled=False)
    assert d.action is PressureAction.WARN
    assert not d.disable_auto_compact


def test_warning_threshold_resets_the_loop_guard() -> None:
    d = assess_pressure(CONTEXT_WARNING_THRESHOLD * 100)
    assert d.compacted_last_turn is False


def test_post_compaction_still_critical_disables_compaction() -> None:
    """A too-small window: compaction cannot get under the threshold."""
    d = post_compaction_still_critical(_CRITICAL_PCT + 5, auto_compact_enabled=True)
    assert d.action is PressureAction.WARN
    assert d.disable_auto_compact is True


def test_post_compaction_success_passes_through() -> None:
    d = post_compaction_still_critical(20.0, auto_compact_enabled=True)
    assert d.action is PressureAction.NONE
    assert not d.disable_auto_compact


def test_ordering_invariant_warn_below_compact_below_critical() -> None:
    """The thresholds must stay ordered, or a band becomes unreachable."""
    assert CONTEXT_WARNING_THRESHOLD < AUTO_COMPACT_THRESHOLD
    assert AUTO_COMPACT_THRESHOLD < CONTEXT_CRITICAL_THRESHOLD
