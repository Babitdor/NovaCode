"""Threshold auto-tuner — Loop-Engineering Enhancement 4 (hill-climbing inward).

The review engine fires on hardcoded thresholds (:data:`config.REVIEW_THRESHOLD_DEFAULT`,
:data:`config.FAILURE_BURST_DEFAULT`). Those defaults suit an "average" session,
but a session that is mostly read-only browsing wants a *higher* review
threshold (less churn), while one full of edits/tests wants a *lower* one (catch
regressions sooner). This module reads the durable trace data Hermes already
records and nudges the thresholds toward the observed working style, writing the
result to :data:`config.HARNESS_CONFIG_NS` where :class:`ReviewRunner` reads it.

Stability
---------
Both signals the tuner keys off are **exogenous** to the thresholds themselves,
so the controller cannot enter a runaway feedback loop:

- ``substantive_ratio`` (fraction of recent tool calls that did real work vs.
  read-only browsing) drives ``review_threshold`` — *inversely*: more real work
  ⇒ review sooner.
- ``failure_rate`` (fraction of recent calls that failed) drives
  ``failure_burst`` — *directly*: when failures are constant background noise a
  burst of 3 fires constantly and wastes reviews, so the burst floor rises.

Each pass blends the suggestion with the current value via
:data:`config.TUNER_DAMPING` (slow convergence, no oscillation) and clamps to the
hard ``*_FLOOR`` / ``*_CEILING`` bounds so the tuner can never starve the user of
reviews nor burn tokens reviewing every other call.

Like the rest of Hermes this is best-effort: any failure logs and returns
``None`` — the agent keeps running on the last-known (or default) thresholds.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from novacode_cli.events import cap_event_log, nova_event_log
from novacode_cli.hermes import config
from novacode_cli.hermes.review import _SUBSTANTIVE_TOOLS, _TRIVIAL_BUILTINS

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("nova.hermes.tuner")

#: Minimum recent tool-history entries before tuning — below this the ratios are
#: too noisy to act on (one unusual command would swing them).
_MIN_HISTORY_FOR_TUNING = 20
#: ``suggested_threshold = DEFAULT * (COEFF - substantive_ratio)``. With COEFF
#: 1.5 the suggestion spans 0.5x..1.5x of the default as the ratio runs 1 down to
#: 0 (all real work -> half the threshold; all browsing -> 1.5x the threshold).
_THRESHOLD_RATIO_COEFF = 1.5


def _clamp(value: int, lo: int, hi: int) -> int:
    """Clamp ``value`` into the inclusive ``[lo, hi]`` range."""
    return max(lo, min(hi, value))


def _is_substantive(tool: str | None) -> bool:
    """Return whether a tool call counts as *real work* (not read-only browsing).

    Mirrors the predicate :meth:`ReviewRunner.should_review` uses, so the tuner's
    notion of "substantive" matches what actually drives a review: an explicit
    write/edit/execute/test, or any non-builtin (skill / MCP) tool.
    """
    if not tool:
        return False
    return tool in _SUBSTANTIVE_TOOLS or tool not in _TRIVIAL_BUILTINS


class ThresholdTuner:
    """Reads Hermes trace data and writes tuned review thresholds to the store.

    Created on demand by :class:`ReviewRunner` (it holds only the durable
    ``store``). A single :meth:`run_tuning_pass` is the whole public surface.
    """

    def __init__(self, store: BaseStore) -> None:
        """Hold the durable store the tuning pass reads from and writes to."""
        self._store = store

    async def run_tuning_pass(self) -> dict[str, Any] | None:
        """Compute and persist tuned thresholds; return the written dict or None.

        Returns ``None`` (no write) when there isn't enough data to tune on
        (fewer than :data:`config.TUNER_MIN_BASIS_REVIEWS` reviews or
        :data:`_MIN_HISTORY_FOR_TUNING` history entries), or on any error.
        """
        try:
            basis_reviews = await self._get_review_count()
            if basis_reviews < config.TUNER_MIN_BASIS_REVIEWS:
                return None

            history = await self._load_history()
            total = len(history)
            if total < _MIN_HISTORY_FOR_TUNING:
                return None

            failures = sum(1 for e in history if not e.get("success", True))
            substantive = sum(1 for e in history if _is_substantive(e.get("tool")))
            failure_rate = failures / total
            substantive_ratio = substantive / total

            old_threshold, old_burst = await self._load_current()

            threshold_signal = _THRESHOLD_RATIO_COEFF - substantive_ratio
            suggested_threshold = _clamp(
                round(config.REVIEW_THRESHOLD_DEFAULT * threshold_signal),
                config.REVIEW_THRESHOLD_FLOOR,
                config.REVIEW_THRESHOLD_CEILING,
            )
            suggested_burst = _clamp(
                round(config.FAILURE_BURST_DEFAULT * (1.0 + failure_rate)),
                config.FAILURE_BURST_FLOOR,
                config.FAILURE_BURST_CEILING,
            )

            keep = 1.0 - config.TUNER_DAMPING
            new_threshold = _clamp(
                round(keep * old_threshold + config.TUNER_DAMPING * suggested_threshold),
                config.REVIEW_THRESHOLD_FLOOR,
                config.REVIEW_THRESHOLD_CEILING,
            )
            new_burst = _clamp(
                round(keep * old_burst + config.TUNER_DAMPING * suggested_burst),
                config.FAILURE_BURST_FLOOR,
                config.FAILURE_BURST_CEILING,
            )

            result: dict[str, Any] = {
                "review_threshold": new_threshold,
                "failure_burst": new_burst,
                "tuned_at": time.time(),
                "basis_reviews": basis_reviews,
            }
            await self._store.aput(config.HARNESS_CONFIG_NS, config.HARNESS_CONFIG_KEY, result)

            if new_threshold != old_threshold or new_burst != old_burst:
                _emit_tuned_event(
                    f"Auto-tuned thresholds: review {old_threshold}→{new_threshold}, "
                    f"failure-burst {old_burst}→{new_burst}"
                )
        except Exception:
            logger.exception("Threshold tuning pass failed")
            return None
        return result

    # -- Store reads --------------------------------------------------------

    async def _get_review_count(self) -> int:
        """Return the total number of completed reviews (the tuning basis size)."""
        try:
            entry = await self._store.aget(("nova", "meta"), "last_review")
            if entry and isinstance(entry.value, dict):
                count = entry.value.get("review_count", 0)
                return int(count) if isinstance(count, int) else 0
        except Exception:
            logger.exception("Tuner failed to read review count")
        return 0

    async def _load_history(self) -> list[dict[str, Any]]:
        """Return the retained tool-history window (``{tool, success, timestamp}``)."""
        try:
            entry = await self._store.aget(("nova", "tool_history"), "history")
            if entry and isinstance(entry.value, dict):
                entries = entry.value.get("entries", [])
                if isinstance(entries, list):
                    return entries
        except Exception:
            logger.exception("Tuner failed to read tool history")
        return []

    async def _load_current(self) -> tuple[int, int]:
        """Return the current ``(review_threshold, failure_burst)`` to blend from.

        Prefers a previously tuned value in the store; falls back to the config
        defaults. Both are clamped so a hand-edited store can't poison the blend.
        """
        threshold = config.REVIEW_THRESHOLD_DEFAULT
        burst = config.FAILURE_BURST_DEFAULT
        try:
            entry = await self._store.aget(config.HARNESS_CONFIG_NS, config.HARNESS_CONFIG_KEY)
            if entry and isinstance(entry.value, dict):
                rt = entry.value.get("review_threshold")
                fb = entry.value.get("failure_burst")
                if isinstance(rt, int):
                    threshold = rt
                if isinstance(fb, int):
                    burst = fb
        except Exception:
            logger.exception("Tuner failed to read current thresholds")
        return (
            _clamp(threshold, config.REVIEW_THRESHOLD_FLOOR, config.REVIEW_THRESHOLD_CEILING),
            _clamp(burst, config.FAILURE_BURST_FLOOR, config.FAILURE_BURST_CEILING),
        )


def _emit_tuned_event(message: str) -> None:
    """Surface a threshold-tuned notice through the TUI-safe event log."""
    try:
        nova_event_log.append(("nova_threshold_tuned", "⇅", "cyan", message))
        cap_event_log()
    except Exception:
        logger.exception("Failed to emit threshold-tuned event")
