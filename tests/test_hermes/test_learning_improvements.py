"""Tests for the NovaLearningMiddleware 'smarter' improvements.

Covers the four upgrades:
1. Skill-effectiveness tracking (real SKILL.md invocation + outcome attribution)
2. Signal-based review triggers (failure burst / substantive window / hard cap)
3. Stronger success signal (content-based failure detection for shell/test)
4. Review memory / no-repeat lessons (prior-lesson digest + MEMORY.md dedup)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novacode_cli.hermes.tracker import ToolUsageTracker
from novacode_cli.hermes.review import ReviewRunner
from novacode_cli.hermes.skill_manager import SkillManager
from novacode_cli.hermes.skill_discovery import check_skill_effectiveness


class FakeStore:
    """Minimal in-memory async store: (namespace, key) -> value dict."""

    def __init__(self) -> None:
        self.data: dict[tuple, dict] = {}

    async def aget(self, namespace, key):
        ns = self.data.get(tuple(namespace), {})
        if key not in ns:
            return None
        return SimpleNamespace(value=dict(ns[key]))

    async def aput(self, namespace, key, value):
        self.data.setdefault(tuple(namespace), {})[key] = dict(value)

    async def asearch(self, namespace):
        ns = self.data.get(tuple(namespace), {})
        return [SimpleNamespace(key=k, value=dict(v)) for k, v in ns.items()]


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def tracker(store):
    return ToolUsageTracker(store, enabled=True)


# ── 1. Skill-effectiveness tracking ──────────────────────────────────────────


class TestSkillInvocationDetection:
    def test_detects_user_skill_read(self):
        tc = {"name": "read_file", "args": {"file_path": "/skills/my-skill/SKILL.md"}}
        assert ToolUsageTracker.skill_name_from_tool_call(tc) == "my-skill"

    def test_detects_claude_skill_read(self):
        tc = {"name": "read_file", "args": {"file_path": "/claude-skills/graphify/SKILL.md"}}
        assert ToolUsageTracker.skill_name_from_tool_call(tc) == "graphify"

    def test_detects_windows_path(self):
        tc = {"name": "read_file", "args": {"file_path": "C:\\x\\skills\\foo\\SKILL.md"}}
        assert ToolUsageTracker.skill_name_from_tool_call(tc) == "foo"

    def test_non_skill_read_returns_none(self):
        tc = {"name": "read_file", "args": {"file_path": "/src/main.py"}}
        assert ToolUsageTracker.skill_name_from_tool_call(tc) is None

    def test_non_read_tool_returns_none(self):
        tc = {"name": "execute", "args": {"file_path": "/skills/x/SKILL.md"}}
        assert ToolUsageTracker.skill_name_from_tool_call(tc) is None

    def test_none_input(self):
        assert ToolUsageTracker.skill_name_from_tool_call(None) is None


class TestSkillUsageAttribution:
    async def test_invocation_counted(self, tracker, store):
        await tracker.record_tool_usage("read_file", True, skill_invoked="my-skill")
        usage = await tracker.get_skill_usage()
        assert usage["my-skill"]["invocations"] == 1

    async def test_subsequent_outcomes_attributed(self, tracker, store):
        await tracker.record_tool_usage("read_file", True, skill_invoked="my-skill")
        await tracker.record_tool_usage("execute", True)
        await tracker.record_tool_usage("execute", False)
        usage = await tracker.get_skill_usage()
        assert usage["my-skill"]["successes"] == 1
        assert usage["my-skill"]["failures"] == 1

    async def test_attribution_budget_expires(self, tracker, store):
        from novacode_cli.hermes.tracker import _SKILL_ATTRIBUTION_BUDGET

        await tracker.record_tool_usage("read_file", True, skill_invoked="my-skill")
        # Far exceed the window; calls past the budget must NOT be attributed.
        for _ in range(_SKILL_ATTRIBUTION_BUDGET + 12):
            await tracker.record_tool_usage("execute", False)
        usage = await tracker.get_skill_usage()
        # Exactly the budget's worth of outcomes attributed, not all of them.
        assert usage["my-skill"]["failures"] == _SKILL_ATTRIBUTION_BUDGET

    async def test_tool_stats_separate_from_skill_usage(self, tracker, store):
        # A non-builtin tool populates tool_stats, not skill_usage.
        await tracker.record_tool_usage("query_project_graph", True)
        assert ("nova", "tool_stats") in store.data
        assert ("nova", "skill_usage") not in store.data


class TestCheckSkillEffectivenessNewSchema:
    async def test_low_usage_not_flagged_for_refinement(self, store):
        # Low usage is handled by the curator (archival), not by rewriting.
        await store.aput(
            ("nova", "skill_usage"),
            "rare",
            {"invocations": 1, "successes": 1, "failures": 0},
        )
        issues = await check_skill_effectiveness(store)
        assert all(name != "rare" for name, _ in issues)

    async def test_high_failure_from_outcomes(self, store):
        await store.aput(
            ("nova", "skill_usage"),
            "bad",
            {"invocations": 4, "successes": 1, "failures": 6},
        )
        issues = await check_skill_effectiveness(store)
        assert ("bad", "high_failure") in issues

    async def test_healthy_skill_not_flagged(self, store):
        await store.aput(
            ("nova", "skill_usage"),
            "good",
            {"invocations": 5, "successes": 9, "failures": 1},
        )
        issues = await check_skill_effectiveness(store)
        assert all(name != "good" for name, _ in issues)

    async def test_barely_used_high_failure_not_flagged(self, store):
        # Below the invocation floor — too little evidence to refine on.
        await store.aput(
            ("nova", "skill_usage"),
            "new",
            {"invocations": 2, "successes": 1, "failures": 6},
        )
        issues = await check_skill_effectiveness(store)
        assert all(name != "new" for name, _ in issues)

    async def test_recently_refined_skill_not_reflagged(self, store):
        # Cooldown: a high-failure skill refined moments ago is skipped.
        import time

        await store.aput(
            ("nova", "skill_usage"),
            "bad",
            {"invocations": 4, "successes": 1, "failures": 6},
        )
        await store.aput(
            ("nova", "skill_refinement"),
            "bad",
            {"last_refined_ts": time.time(), "refine_count": 1},
        )
        issues = await check_skill_effectiveness(store)
        assert all(name != "bad" for name, _ in issues)

    async def test_refine_attempt_cap_stops_reflagging(self, store):
        from novacode_cli.hermes.skill_discovery import _MAX_REFINES

        await store.aput(
            ("nova", "skill_usage"),
            "bad",
            {"invocations": 9, "successes": 1, "failures": 8},
        )
        await store.aput(
            ("nova", "skill_refinement"),
            "bad",
            {"last_refined_ts": 0.0, "refine_count": _MAX_REFINES},
        )
        issues = await check_skill_effectiveness(store)
        assert all(name != "bad" for name, _ in issues)

    async def test_record_refinement_resets_counters_and_stamps_record(self, store):
        from novacode_cli.hermes.skill_discovery import _record_refinement

        await store.aput(
            ("nova", "skill_usage"),
            "bad",
            {
                "invocations": 5,
                "successes": 1,
                "failures": 6,
                "failure_samples": [{"tool": "x", "excerpt": "boom"}],
            },
        )
        await _record_refinement(store, "bad", "high_failure")

        usage = (await store.aget(("nova", "skill_usage"), "bad")).value
        assert usage["successes"] == 0
        assert usage["failures"] == 0
        assert usage["failure_samples"] == []
        assert usage["invocations"] == 5  # invocations are preserved

        rec = (await store.aget(("nova", "skill_refinement"), "bad")).value
        assert rec["refine_count"] == 1
        assert rec["invocations_at_refine"] == 5
        assert rec["last_refined_ts"] > 0

        # The reset means the skill is no longer flagged until fresh evidence accrues.
        assert await check_skill_effectiveness(store) == []


# ── 2. Signal-based review triggers ──────────────────────────────────────────


def _make_review(store, threshold=4):
    tracker = ToolUsageTracker(store, enabled=True)
    sm = SkillManager(store, enabled=True)
    # These classes pin the count-and-signal triggers, which are the "periodic"
    # mode. The default ("task_end") is covered in tests/test_review_task_end.py.
    return ReviewRunner(
        store, tracker, sm, review_threshold=threshold, enabled=True, mode="periodic"
    )


async def _seed_window(store, entries):
    await store.aput(("nova", "tool_history"), "history", {"entries": entries})
    await store.aput(("nova", "tool_counter"), "counter", {"count": len(entries)})


class TestSignalBasedTriggers:
    async def test_all_trivial_window_defers(self, store):
        review = _make_review(store, threshold=4)
        await _seed_window(store, [{"tool": "read_file", "success": True}] * 4)
        assert await review.should_review() is False

    async def test_substantive_window_triggers(self, store):
        review = _make_review(store, threshold=4)
        window = [{"tool": "read_file", "success": True}] * 3 + [
            {"tool": "edit_file", "success": True}
        ]
        await _seed_window(store, window)
        assert await review.should_review() is True

    async def test_failure_burst_triggers_early(self, store):
        review = _make_review(store, threshold=10)
        # Below threshold (10) but a burst of failures and above min_floor.
        window = [{"tool": "execute", "success": False}] * 5
        await _seed_window(store, window)
        assert await review.should_review() is True

    async def test_hard_cap_triggers_even_if_trivial(self, store):
        review = _make_review(store, threshold=4)
        # 2x threshold of pure reads — must not defer forever.
        await _seed_window(store, [{"tool": "read_file", "success": True}] * 8)
        assert await review.should_review() is True

    async def test_below_floor_no_trigger(self, store):
        review = _make_review(store, threshold=4)
        await _seed_window(store, [{"tool": "edit_file", "success": True}] * 2)
        assert await review.should_review() is False

    async def test_just_completed_skips_one(self, store):
        review = _make_review(store, threshold=4)
        window = [{"tool": "edit_file", "success": True}] * 4
        await _seed_window(store, window)
        await store.aput(("nova", "meta"), "review_just_completed", {"value": True})
        assert await review.should_review() is False  # skipped
        assert await review.should_review() is True  # flag cleared


class TestCleanWinTrigger:
    async def test_clean_substantive_window_triggers_below_threshold(self, store):
        # threshold=10 -> min_floor=5. 5 clean substantive calls, no failures,
        # below the substantive threshold and below the failure-burst count.
        review = _make_review(store, threshold=10)
        await _seed_window(store, [{"tool": "edit_file", "success": True}] * 5)
        assert await review.should_review() is True
        assert review._pending_clean_win is True

    async def test_window_with_failure_is_not_clean_win(self, store):
        review = _make_review(store, threshold=10)
        window = [{"tool": "edit_file", "success": True}] * 4 + [
            {"tool": "execute", "success": False}
        ]
        await _seed_window(store, window)  # 5 calls, 1 failure, below threshold
        assert await review.should_review() is False

    async def test_all_trivial_window_is_not_clean_win(self, store):
        review = _make_review(store, threshold=10)
        await _seed_window(store, [{"tool": "read_file", "success": True}] * 5)
        assert await review.should_review() is False


# ── 3. Stronger success signal ───────────────────────────────────────────────


class TestExecutionFailureDetection:
    def test_traceback_is_failure(self):
        from novacode_cli.hermes.middleware import _execution_failed

        resp = SimpleNamespace(content="Traceback (most recent call last):\n  ...\nValueError")
        assert _execution_failed("execute", resp) is True

    def test_clean_output_is_success(self):
        from novacode_cli.hermes.middleware import _execution_failed

        resp = SimpleNamespace(content="All 12 tests passed. 0 failed.")
        assert _execution_failed("execute", resp) is False

    def test_n_failed_is_failure(self):
        from novacode_cli.hermes.middleware import _execution_failed

        resp = SimpleNamespace(content="3 failed, 9 passed in 1.2s")
        assert _execution_failed("execute", resp) is True

    def test_non_execute_tool_ignored(self):
        from novacode_cli.hermes.middleware import _execution_failed

        resp = SimpleNamespace(content="Traceback (most recent call last):")
        assert _execution_failed("read_file", resp) is False

    def test_command_not_found(self):
        from novacode_cli.hermes.middleware import _execution_failed

        resp = SimpleNamespace(content="bash: foo: command not found")
        assert _execution_failed("execute", resp) is True

    def test_list_content_flattened(self):
        from novacode_cli.hermes.middleware import _execution_failed

        resp = SimpleNamespace(content=[{"type": "text", "text": "npm ERR! boom"}])
        assert _execution_failed("execute", resp) is True


# ── 4. Review memory / no-repeat lessons ─────────────────────────────────────


class TestRecentLessons:
    async def test_digest_orders_newest_first(self, store):
        await store.aput(("nova", "reviews"), "r1", {"timestamp": 1.0, "content": "older"})
        await store.aput(("nova", "reviews"), "r2", {"timestamp": 2.0, "content": "newer"})
        review = _make_review(store)
        digest = await review._recent_lessons()
        assert digest.index("newer") < digest.index("older")

    async def test_empty_when_no_reviews(self, store):
        review = _make_review(store)
        assert await review._recent_lessons() == ""


class TestErrorRecoveryNudge:
    def test_no_window_no_recovery(self):
        from novacode_cli.hermes.review import _window_recovered

        assert _window_recovered([]) is False

    def test_all_success_no_recovery(self):
        from novacode_cli.hermes.review import _window_recovered

        w = [{"tool": "edit_file", "success": True}] * 3
        assert _window_recovered(w) is False

    def test_failure_then_substantive_success_is_recovery(self):
        from novacode_cli.hermes.review import _window_recovered

        w = [
            {"tool": "execute", "success": False},
            {"tool": "edit_file", "success": True},
            {"tool": "execute", "success": True},
        ]
        assert _window_recovered(w) is True

    def test_failure_then_only_trivial_is_not_recovery(self):
        from novacode_cli.hermes.review import _window_recovered

        w = [
            {"tool": "execute", "success": False},
            {"tool": "read_file", "success": True},
        ]
        assert _window_recovered(w) is False

    def test_failure_last_is_not_recovery(self):
        from novacode_cli.hermes.review import _window_recovered

        w = [
            {"tool": "edit_file", "success": True},
            {"tool": "execute", "success": False},
        ]
        assert _window_recovered(w) is False

    def test_review_prompt_includes_recovery_nudge(self):
        from novacode_cli.prompts import render_template

        out = render_template(
            "nova_review.jinja",
            tool_call_count=10,
            prior_lessons="",
            recovered_from_error=True,
        )
        assert "recovered from an error" in out

    def test_review_prompt_omits_nudge_when_no_recovery(self):
        from novacode_cli.prompts import render_template

        out = render_template(
            "nova_review.jinja",
            tool_call_count=10,
            prior_lessons="",
            recovered_from_error=False,
        )
        assert "recovered from an error" not in out


class TestMemoryDedup:
    def test_dedup_drops_existing_bullet(self):
        from novacode_cli.hermes.memory_tiers import _dedup_against

        existing = "## Notes\n- Tests run with pytest -x\n"
        block = "- Tests run with pytest -x\n- New fact about caching"
        result = _dedup_against(existing, block)
        assert "New fact about caching" in result
        assert result.count("pytest -x") == 0

    def test_dedup_keeps_headers_and_prose(self):
        from novacode_cli.hermes.memory_tiers import _dedup_against

        result = _dedup_against("- a", "## Header\nSome prose\n- a\n- b")
        assert "## Header" in result
        assert "Some prose" in result
        assert "- b" in result

    def test_update_from_review_skips_all_duplicates(self, tmp_path):
        from novacode_cli.hermes.memory_tiers import update_from_review

        agent_dir = tmp_path
        topic_file = agent_dir / "memories" / "lessons.md"
        topic_file.parent.mkdir(parents=True, exist_ok=True)
        topic_file.write_text(
            "# Lessons\n\n## Review — earlier\n\n- alpha fact\n", encoding="utf-8"
        )
        update_from_review(agent_dir, "", [{"topic": "lessons", "bullets": "- alpha fact"}])
        content = topic_file.read_text(encoding="utf-8")
        # No new "## Review" section appended because the only bullet was a dup.
        assert content.count("## Review") == 1
