"""Nova's internal and displayed compaction thresholds must stay in sync.

Two compactors run against the same conversation:

  * Nova's auto-compact (between turns, visible in the transcript, loop-guarded)
  * deepagents' ``SummarizationMiddleware`` (mid-turn, inside the graph)

Nova replaces the library middleware by its public name and configures it at the
same effective threshold shown by ``/context``. The model profile continues to
report the actual context window.

These tests pin the invariant and the shared window.

Runnable directly (``python tests/test_compaction_trigger_sync.py``).
"""

from __future__ import annotations

import inspect

from novacode_cli.context._analysis import (
    AUTO_COMPACT_THRESHOLD,
    CONTEXT_WARNING_THRESHOLD,
    LIB_SUMMARIZATION_FRACTION,
    ContextBreakdown,
)

# ── the ordering invariant ───────────────────────────────────────────────────


def test_internal_compaction_uses_the_displayed_threshold_for_each_window():
    from novacode_cli.agents.core_agent import _context_summarization_trigger
    from novacode_cli.context import compact_threshold_pct

    for window in (40_960, 128_000, 200_000):
        assert _context_summarization_trigger(window) == (
            "fraction",
            compact_threshold_pct(window) / 100,
        )


def test_auto_compact_is_above_the_warning_line():
    # Otherwise we'd compact before ever warning the user.
    assert AUTO_COMPACT_THRESHOLD > CONTEXT_WARNING_THRESHOLD


def test_library_fraction_matches_installed_deepagents():
    """LIB_SUMMARIZATION_FRACTION must track the installed library's real value.

    Pin the upstream default so dependency changes prompt a configuration review.
    """
    from deepagents.middleware import summarization as summ

    src = inspect.getsource(summ)
    assert f'"trigger": ("fraction", {LIB_SUMMARIZATION_FRACTION})' in src, (
        "deepagents' summarization fraction changed — update "
        "LIB_SUMMARIZATION_FRACTION and review Nova's override"
    )


# ── the threshold actually drives the breakdown ──────────────────────────────


def _bd(pct: float, window: int = 128_000) -> ContextBreakdown:
    # A real-sized window: below ~16K the absolute headroom reserve binds
    # instead of the percentage (see compact_threshold_pct).
    return ContextBreakdown(total_tokens=round(pct / 100 * window), context_window_size=window)


def test_small_windows_compact_earlier_to_keep_headroom():
    """82% of 40K leaves 7.4K free — less than one large file read."""
    from novacode_cli.context import MIN_RESERVE_TOKENS, compact_threshold_pct

    assert compact_threshold_pct(128_000) == AUTO_COMPACT_THRESHOLD * 100
    assert compact_threshold_pct(40_960) < AUTO_COMPACT_THRESHOLD * 100
    assert _bd(81, window=40_960).should_auto_compact is True
    free = 40_960 - round(compact_threshold_pct(40_960) / 100 * 40_960)
    assert free >= MIN_RESERVE_TOKENS - 1


def test_should_auto_compact_fires_at_threshold():
    assert _bd(AUTO_COMPACT_THRESHOLD * 100).should_auto_compact is True
    assert _bd(AUTO_COMPACT_THRESHOLD * 100 - 1).should_auto_compact is False


def test_internal_compaction_threshold_applies_small_window_reserve():
    from novacode_cli.agents.core_agent import _context_summarization_trigger
    from novacode_cli.context import compact_threshold_pct

    trigger = _context_summarization_trigger(40_960)
    assert trigger[1] * 100 == compact_threshold_pct(40_960)


def test_configured_summarizer_replaces_deepagents_default_by_name():
    """Pin the graph merge behavior used to supply Nova's threshold."""
    from deepagents.backends import StateBackend
    from deepagents.graph import _apply_custom_middleware
    from deepagents.middleware.summarization import (
        SummarizationMiddleware,
        create_summarization_middleware,
    )
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.core_agent import _context_summarization_trigger

    model = FakeListChatModel(responses=["summary"])
    model.profile = {"max_input_tokens": 128_000}
    backend = StateBackend()
    default = create_summarization_middleware(model, backend)
    configured = SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=_context_summarization_trigger(128_000),
        keep=("fraction", 0.10),
    )

    merged = _apply_custom_middleware([default], [configured])
    assert len(merged) == 1
    assert merged[0] is configured


def test_auto_compact_precedes_critical():
    # Compaction should happen before the red "critical" indicator, not after.
    assert _bd(85).should_auto_compact is True
    assert _bd(85).is_critical is False


# ── the shared window ────────────────────────────────────────────────────────


def test_profile_seeding_overwrites_a_model_supplied_window():
    """Both compactors must measure the SAME window.

    A model that ships its own profile (ChatOpenAI's 128K for gpt-4o) used to be
    skipped, so the library summarized on the model's number while the indicator
    showed Nova's — compaction then looked like it fired for no reason.
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.core_agent import _seed_summarization_profile
    from novacode_cli.context import ContextManager

    model = FakeListChatModel(responses=["hi"])
    model.profile = {"max_input_tokens": 999, "max_output_tokens": 4096}

    _seed_summarization_profile(model, "claude-opus-4-8")

    expected = ContextManager("claude-opus-4-8").window_size()
    assert model.profile["max_input_tokens"] == expected
    # Unrelated profile keys survive.
    assert model.profile["max_output_tokens"] == 4096


def test_profile_seeding_creates_profile_when_absent():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from novacode_cli.agents.core_agent import _seed_summarization_profile
    from novacode_cli.context import ContextManager

    model = FakeListChatModel(responses=["hi"])
    _seed_summarization_profile(model, "claude-opus-4-8")
    assert model.profile["max_input_tokens"] == ContextManager("claude-opus-4-8").window_size()


def test_profile_seeding_ignores_non_chat_models():
    from novacode_cli.agents.core_agent import _seed_summarization_profile

    sentinel = object()
    _seed_summarization_profile(sentinel, "claude-opus-4-8")  # must not raise
    assert not hasattr(sentinel, "profile")


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v", "--assert=plain"]))


def test_clearing_always_precedes_compaction():
    """The cheap reducer must get its chance first, on every window size."""
    from novacode_cli.agents.core_agent import _context_edit_trigger
    from novacode_cli.context import compact_threshold_pct

    for window in (8_192, 16_384, 40_960, 128_000, 200_000):
        compact_at = window * compact_threshold_pct(window) / 100
        assert _context_edit_trigger(window) < compact_at, window
