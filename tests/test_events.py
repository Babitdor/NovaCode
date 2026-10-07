"""Tests for novacode_cli.events — shared event buffer and cap function."""

import pytest

from novacode_cli.events import _MAX_EVENT_LOG, cap_event_log, nova_event_log


class TestNovaEventLog:
    """Tests for the module-level event log and cap_event_log()."""

    def setup_method(self):
        """Clear the event log before each test."""
        nova_event_log.clear()

    def test_empty_by_default(self):
        assert len(nova_event_log) == 0

    def test_appended_entries_present(self):
        nova_event_log.append(("test_type", "🔍", "yellow", "test entry"))
        assert len(nova_event_log) == 1
        assert nova_event_log[0][0] == "test_type"

    def test_cap_does_not_trim_when_under_limit(self):
        for i in range(10):
            nova_event_log.append(("test", "🔍", "yellow", str(i)))
        cap_event_log()
        assert len(nova_event_log) == 10

    def test_cap_trims_to_max_when_over_limit(self):
        """When log exceeds _MAX_EVENT_LOG, cap_event_log trims the oldest."""
        overage = 50
        for i in range(_MAX_EVENT_LOG + overage):
            nova_event_log.append(("test", "🔍", "yellow", str(i)))
        cap_event_log()
        assert len(nova_event_log) == _MAX_EVENT_LOG

    def test_cap_keeps_newest_entries(self):
        """After capping, the most recent entries survive."""
        for i in range(_MAX_EVENT_LOG + 10):
            nova_event_log.append(("test", "🔍", "yellow", str(i)))
        cap_event_log()
        # The last 200 entries should be indices 10..209
        expected_first = 10
        first_msg = nova_event_log[0][3]
        assert first_msg == str(expected_first), (
            f"Expected first entry message '{expected_first}', got '{first_msg}'"
        )

    def test_buffer_caps_uncapped_producer_appends_automatically(self):
        for i in range(_MAX_EVENT_LOG + 7):
            nova_event_log.append(("background", "•", "cyan", str(i)))

        assert len(nova_event_log) == _MAX_EVENT_LOG
        assert nova_event_log[0][3] == "7"

    def test_extend_also_enforces_the_buffer_limit(self):
        nova_event_log.extend(
            ("background", "•", "cyan", str(i))
            for i in range(_MAX_EVENT_LOG + 7)
        )

        assert len(nova_event_log) == _MAX_EVENT_LOG
        assert nova_event_log[0][3] == "7"

    def test_cap_is_idempotent(self):
        """Calling cap_event_log twice is safe."""
        for i in range(_MAX_EVENT_LOG + 30):
            nova_event_log.append(("test", "🔍", "yellow", str(i)))
        cap_event_log()
        first_len = len(nova_event_log)
        cap_event_log()
        assert len(nova_event_log) == first_len

    def test_empty_log_cap_is_noop(self):
        nova_event_log.clear()
        cap_event_log()
        assert len(nova_event_log) == 0

    def test_exactly_at_limit_no_trim(self):
        for i in range(_MAX_EVENT_LOG):
            nova_event_log.append(("test", "🔍", "yellow", str(i)))
        cap_event_log()
        assert len(nova_event_log) == _MAX_EVENT_LOG
