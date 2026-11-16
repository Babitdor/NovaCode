"""Quit/teardown must stay bounded even if a background service hangs.

The real ``ProcessManager`` singleton is monkeypatched
to a no-op so the test stays hermetic (it touches global fds that would otherwise
corrupt pytest's output capture) and only the timeout-bounding logic is exercised.
"""

import asyncio
import time

import novacode_cli.main as m


def _patch_globals(monkeypatch):
    class _Inst:
        async def stop_all(self):
            return 0

    class _PM:
        @staticmethod
        def get_instance():
            return _Inst()

    monkeypatch.setattr(m, "ProcessManager", _PM)


class _HangBridge:
    async def stop_all(self):
        await asyncio.sleep(60)  # simulates a wedged Discord/Telegram socket


class _HangTrello:
    async def stop(self):
        await asyncio.sleep(60)


def test_shutdown_is_bounded_when_services_hang(monkeypatch):
    _patch_globals(monkeypatch)

    class _SS:
        def __init__(self):
            self._remote_bridge_manager = _HangBridge()
            self.trello_server = _HangTrello()
            self._remote_processor_task = None

    start = time.monotonic()
    asyncio.run(m._shutdown_background_services(_SS()))
    elapsed = time.monotonic() - start
    # Per-step guard (4s) + overall gather cap (8s) — must NOT wait on the 60s
    # hangs. Generous upper bound to avoid flakiness on slow machines.
    assert elapsed < 12, f"shutdown took {elapsed:.1f}s (a hung service blocked quit)"


def test_shutdown_no_services_is_fast(monkeypatch):
    _patch_globals(monkeypatch)

    class _Empty:
        _remote_bridge_manager = None
        trello_server = None
        _remote_processor_task = None

    start = time.monotonic()
    asyncio.run(m._shutdown_background_services(_Empty()))
    assert time.monotonic() - start < 3
