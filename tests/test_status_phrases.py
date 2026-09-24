"""Tests for the rotating status phrases.

Pins two contracts that matter beyond cosmetics:

* ``sticky`` must NOT change between calls. The TUI guards its status rebuild
  with ``if self._activity != <phrase>``, so a phrase that re-rolled per call
  would rebuild the status line on every streamed token.
* Every phrase must be reachable, so a turn eventually shows variety.
"""

from __future__ import annotations

import pytest

from novacode_cli.ui import status_phrases


@pytest.fixture(autouse=True)
def _reset_cursors() -> None:
    """Isolate each test from the module-level cursors."""
    status_phrases.reset()
    status_phrases._cursor.clear()
    yield
    status_phrases.reset()
    status_phrases._cursor.clear()


def test_rotating_advances_and_covers_every_phrase():
    pool = status_phrases._POOLS["thinking"]
    seen = [status_phrases.rotating("thinking") for _ in range(len(pool))]
    assert len(set(seen)) == len(pool), "rotation repeated before covering the pool"
    assert set(seen) == set(pool), "rotation did not reach every phrase"


def test_sticky_is_stable_across_calls():
    """The TUI's per-token status guard depends on this."""
    first = status_phrases.sticky("thinking")
    assert [status_phrases.sticky("thinking") for _ in range(20)] == [first] * 20


def test_reset_makes_sticky_pick_something_new():
    first = status_phrases.sticky("thinking")
    status_phrases.reset("thinking")
    # The cursor advanced past `first`, so the next pick differs.
    assert status_phrases.sticky("thinking") != first


def test_reset_all_clears_every_category():
    status_phrases.sticky("thinking")
    status_phrases.sticky("responding")
    status_phrases.reset()
    assert status_phrases._stuck == {}


def test_status_line_prefixes_the_agent_name():
    line = status_phrases.status_line("subagent", "researcher")
    assert line.startswith("researcher ")
    assert line[len("researcher ") :] in status_phrases._POOLS["subagent"]


def test_unknown_category_does_not_raise():
    """An unknown pool is a programming error, but the UI path must not die."""
    assert status_phrases.rotating("no-such-category") in status_phrases._POOLS["thinking"]


def test_every_phrase_reads_as_a_verb_phrase():
    """Each phrase completes '{agent} ___', so it must start with 'is '."""
    for category, pool in status_phrases._POOLS.items():
        for phrase in pool:
            assert phrase.startswith("is "), f"{category}: {phrase!r} does not start with 'is '"
            assert phrase.endswith("…"), f"{category}: {phrase!r} does not end with '…'"
