"""Tests for the cron / heartbeat scheduler (Loop-Engineering Enhancement 3)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from novacode_cli.hermes import config
from novacode_cli.remote.bridge import RemotePlatform
from novacode_cli.remote.scheduler import (
    CronScheduler,
    cron_matches,
    parse_cron,
)


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple, dict] = {}

    async def aget(self, namespace, key):
        ns = self.data.get(tuple(namespace), {})
        return SimpleNamespace(value=dict(ns[key])) if key in ns else None

    async def aput(self, namespace, key, value):
        self.data.setdefault(tuple(namespace), {})[key] = dict(value)

    async def adelete(self, namespace, key):
        self.data.get(tuple(namespace), {}).pop(key, None)

    async def asearch(self, namespace, *, limit=10, offset=0):
        ns = self.data.get(tuple(namespace), {})
        return [SimpleNamespace(key=k, value=dict(v)) for k, v in ns.items()][offset:offset + limit]


# ── cron parsing ─────────────────────────────────────────────────────────────


class TestCronParsing:
    def test_wildcards(self):
        minute, hour, dom, month, dow = parse_cron("* * * * *")
        assert minute == set(range(60))
        assert hour == set(range(24))
        assert month == set(range(1, 13))

    def test_specific_and_step_and_range_and_list(self):
        minute, hour, _, _, _ = parse_cron("0,30 */6 * * *")
        assert minute == {0, 30}
        assert hour == {0, 6, 12, 18}
        _, _, _, _, dow = parse_cron("* * * * 1-5")
        assert dow == {1, 2, 3, 4, 5}

    def test_sunday_seven_alias(self):
        _, _, _, _, dow = parse_cron("* * * * 7")
        assert dow == {0}

    @pytest.mark.parametrize("bad", ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *"])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            parse_cron(bad)

    def test_matches_minute_and_dow(self):
        now = datetime(2026, 6, 18, 9, 30)  # noqa: DTZ001
        dow = now.isoweekday() % 7
        assert cron_matches("30 9 * * *", now)
        assert cron_matches(f"30 9 * * {dow}", now)
        assert not cron_matches("31 9 * * *", now)
        assert not cron_matches(f"30 9 * * {(dow + 1) % 7}", now)


# ── scheduler job management ─────────────────────────────────────────────────


class TestSchedulerJobs:
    async def test_add_validates_expression(self):
        sched = CronScheduler(asyncio.Queue(), store=FakeStore())
        with pytest.raises(ValueError):
            await sched.add_job("not a cron", "task")

    async def test_add_persists_and_lists(self):
        store = FakeStore()
        sched = CronScheduler(asyncio.Queue(), store=store)
        jid = await sched.add_job("0 9 * * *", "morning review")
        assert any(j["job_id"] == jid for j in sched.list_jobs())
        # Persisted to the store namespace.
        assert store.data[config.CRON_SCHEDULES_NS][jid]["task"] == "morning review"

    async def test_remove(self):
        store = FakeStore()
        sched = CronScheduler(asyncio.Queue(), store=store)
        jid = await sched.add_job("0 9 * * *", "task")
        assert await sched.remove_job(jid) is True
        assert sched.list_jobs() == []
        assert jid not in store.data.get(config.CRON_SCHEDULES_NS, {})
        assert await sched.remove_job("nope") is False

    async def test_load_resumes_persisted_jobs(self):
        store = FakeStore()
        first = CronScheduler(asyncio.Queue(), store=store)
        await first.add_job("0 9 * * *", "persisted task")
        # A fresh scheduler pointed at the same store recovers the job.
        second = CronScheduler(asyncio.Queue(), store=store)
        await second._load_jobs()
        assert len(second.list_jobs()) == 1
        assert second.list_jobs()[0]["task"] == "persisted task"


# ── firing ───────────────────────────────────────────────────────────────────


class TestSchedulerFiring:
    async def test_fire_now_enqueues_cron_message(self):
        queue: asyncio.Queue = asyncio.Queue()
        sched = CronScheduler(queue, store=FakeStore())
        await sched.fire_now("do the thing")
        msg = queue.get_nowait()
        assert msg.platform is RemotePlatform.CRON
        assert msg.text == "do the thing"

    async def test_tick_fires_matching_job_once_per_minute(self):
        queue: asyncio.Queue = asyncio.Queue()
        sched = CronScheduler(queue, store=FakeStore())
        now = datetime(2026, 6, 18, 9, 30)  # noqa: DTZ001
        await sched.add_job("30 9 * * *", "review")

        await sched._tick(now)
        assert queue.qsize() == 1
        # Same minute → no duplicate.
        await sched._tick(now)
        assert queue.qsize() == 1
        # Next matching minute (a day later) → fires again.
        await sched._tick(datetime(2026, 6, 19, 9, 30))  # noqa: DTZ001
        assert queue.qsize() == 2

    async def test_tick_skips_non_matching_job(self):
        queue: asyncio.Queue = asyncio.Queue()
        sched = CronScheduler(queue, store=FakeStore())
        await sched.add_job("0 0 * * *", "midnight only")
        await sched._tick(datetime(2026, 6, 18, 9, 30))  # noqa: DTZ001
        assert queue.qsize() == 0

    async def test_noop_reply_is_callable(self):
        # The CRON message must carry a usable reply sink for the processor.
        queue: asyncio.Queue = asyncio.Queue()
        sched = CronScheduler(queue, store=FakeStore())
        await sched.fire_now("x")
        msg = queue.get_nowait()
        await msg.reply_fn("anything")  # no raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
