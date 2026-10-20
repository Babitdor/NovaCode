"""Cleanup proves process ownership and never signals reused or foreign PIDs."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import psutil

from novacode_cli.teams.recovery import RecoveryHandle, stop_process
from novacode_cli.teams.scheduler import PROCESS_BUDGET


async def test_recovery_verifies_birth_and_nonce_before_stopping_real_process():
    nonce = uuid.uuid4().hex
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        env={**os.environ, "NOVA_TEAM_ATTEMPT": nonce},
    )
    born = psutil.Process(process.pid).create_time()
    record = {"kind": "process", "pid": process.pid, "born": born, "attempt": nonce}
    initial = PROCESS_BUDGET.used
    try:
        assert await asyncio.to_thread(stop_process, {**record, "born": born + 1})
        assert process.returncode is None
        handle = RecoveryHandle({**record, "attempt": "foreign"})
        assert not await handle.stop()
        assert process.returncode is None
        assert PROCESS_BUDGET.used == initial + 1
        handle.recovery = record
        assert await handle.stop()
        await asyncio.wait_for(process.wait(), timeout=5)
        assert PROCESS_BUDGET.used == initial
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
