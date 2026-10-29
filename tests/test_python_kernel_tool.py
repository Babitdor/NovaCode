"""Persistent Python kernel tool: namespace persistence, snapshots, timeouts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from novacode_cli.tools.python_kernel_tool import _get_kernel, python_kernel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture(autouse=True)
async def _fresh_kernel() -> AsyncIterator[None]:
    """Ensure a clean kernel per test (and close it after)."""
    kernel = await _get_kernel()
    await kernel.request({"op": "reset"}, timeout_secs=10)
    yield
    await kernel.close()


class TestPersistence:
    async def test_state_persists_across_calls(self):
        r1 = await python_kernel.ainvoke({"code": "x = 41"})
        assert "Error" not in r1
        r2 = await python_kernel.ainvoke({"code": "x + 1"})
        assert "42" in r2

    async def test_imports_persist(self):
        await python_kernel.ainvoke({"code": "import math"})
        r = await python_kernel.ainvoke({"code": "math.sqrt(16)"})
        assert "4.0" in r

    async def test_print_captured(self):
        r = await python_kernel.ainvoke({"code": "print('hello kernel')"})
        assert "hello kernel" in r

    async def test_no_output_returns_placeholder(self):
        r = await python_kernel.ainvoke({"code": "x = 1"})
        assert "(no output)" in r


class TestErrors:
    async def test_error_returns_traceback_and_namespace_survives(self):
        r = await python_kernel.ainvoke({"code": "1 / 0"})
        assert "Error" in r
        assert "ZeroDivisionError" in r
        # The namespace survives the error.
        await python_kernel.ainvoke({"code": "y = 5"})
        r2 = await python_kernel.ainvoke({"code": "y * 2"})
        assert "10" in r2

    async def test_reset_clears_namespace(self):
        await python_kernel.ainvoke({"code": "z = 99"})
        await python_kernel.ainvoke({"code": "reset"})
        r = await python_kernel.ainvoke({"code": "z"})
        assert "Error" in r  # NameError: z is gone


class TestSnapshots:
    async def test_save_and_load_roundtrip(self, tmp_path: Path):
        # Save a namespace with a lambda (dill-only, pickle would fail).
        r = await python_kernel.ainvoke(
            {
                "code": "f = lambda n: n * 3",
                "snapshot": "save",
                "snapshot_path": str(tmp_path / "ns.pkl"),
            }
        )
        assert "snapshot saved" in r
        assert (tmp_path / "ns.pkl").exists()

        # Reset, then load the snapshot and use the lambda.
        await python_kernel.ainvoke({"code": "reset"})
        r2 = await python_kernel.ainvoke(
            {
                "code": "f(7)",
                "snapshot": f"load:{tmp_path / 'ns.pkl'}",
            }
        )
        assert "21" in r2

    async def test_load_missing_snapshot_errors(self):
        r = await python_kernel.ainvoke({"code": "x = 1", "snapshot": "load:/nonexistent/ns.pkl"})
        assert "Error" in r
        assert "load failed" in r


class TestTimeout:
    async def test_hung_kernel_times_out_and_respawns(self):
        r = await python_kernel.ainvoke({"code": "import time; time.sleep(999)", "timeout": 1.0})
        assert "timed out" in r
        # Next call works on a fresh kernel.
        r2 = await python_kernel.ainvoke({"code": "1 + 1"})
        assert "2" in r2


async def test_parallel_requests_keep_their_responses():
    kernel = await _get_kernel()
    results = await asyncio.gather(
        *(
            kernel.request({"code": f"import time; time.sleep(0.05); print({i})"}, 5)
            for i in range(4)
        )
    )
    assert [result["output"].strip() for result in results] == ["0", "1", "2", "3"]


async def test_cancelled_request_discards_child_and_stale_response():
    kernel = await _get_kernel()
    task = asyncio.create_task(
        kernel.request({"code": "import time; time.sleep(0.3); print('stale')"}, 5)
    )
    await asyncio.sleep(0.1)
    proc = kernel._proc
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.returncode is not None
    result = await kernel.request({"code": "print('fresh')"}, 5)
    assert result["output"].strip() == "fresh"


async def test_kernel_namespaces_belong_to_project_session(tmp_path: Path):
    from novacode_cli.session.runtime_identity import RuntimeIdentity, current_identity

    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    token = current_identity.set(
        RuntimeIdentity.from_config(str(first_dir), {"configurable": {"thread_id": "a"}}, "")
    )
    first = await _get_kernel()
    second = None
    try:
        await first.request({"code": "secret = 42"}, 5)
        other = current_identity.set(
            RuntimeIdentity.from_config(str(second_dir), {"configurable": {"thread_id": "a"}}, "")
        )
        try:
            second = await _get_kernel()
            result = await second.request({"code": "print('secret' in globals())"}, 5)
            assert result["output"].strip() == "False"
        finally:
            current_identity.reset(other)
        assert await _get_kernel() is first
    finally:
        await first.close()
        if second is not None:
            await second.close()
        current_identity.reset(token)


async def test_cancelling_queued_request_keeps_active_kernel():
    kernel = await _get_kernel()
    active = asyncio.create_task(
        kernel.request({"code": "import time; time.sleep(0.3); print('active')"}, 5)
    )
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(kernel.request({"code": "print('queued')"}, 5))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    result = await active
    assert result["output"].strip() == "active"
    assert kernel._proc.returncode is None


async def test_close_terminates_an_active_request():
    kernel = await _get_kernel()
    active = asyncio.create_task(kernel.request({"code": "import time; time.sleep(999)"}, 1000))
    await asyncio.sleep(0.05)
    proc = kernel._proc
    try:
        await asyncio.wait_for(kernel.close(), 2)
        assert proc.returncode is not None
        assert active.done()
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


async def test_invalid_protocol_cannot_become_next_calls_result():
    first = await python_kernel.ainvoke(
        {
            "code": "import sys; sys.__stdout__.write('invalid\\n'); sys.__stdout__.flush()",
        }
    )
    assert "invalid JSON" in first
    second = await python_kernel.ainvoke({"code": "print('fresh')"})
    assert second == "fresh"


async def test_exited_kernel_reports_error_and_next_call_recovers():
    result = await python_kernel.ainvoke({"code": "import os; os._exit(7)"})
    assert "kernel exited unexpectedly" in result
    assert await python_kernel.ainvoke({"code": "print('recovered')"}) == "recovered"


async def test_closed_kernel_is_not_reused():
    kernel = await _get_kernel()
    await kernel.close()
    with pytest.raises(RuntimeError, match="closed"):
        await kernel.request({"code": "1"}, 5)
    replacement = await _get_kernel()
    assert replacement is not kernel
    try:
        assert (await replacement.request({"code": "print('new')"}, 5))["output"].strip() == "new"
    finally:
        await replacement.close()


async def test_live_kernel_rejects_foreign_event_loop():
    kernel = await _get_kernel()
    with pytest.raises(RuntimeError, match="event loops"):
        await asyncio.to_thread(lambda: asyncio.run(kernel.request({"code": "1"}, 5)))
    assert (await kernel.request({"code": "print('owner')"}, 5))["output"].strip() == "owner"
