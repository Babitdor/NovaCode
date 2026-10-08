"""Run a two-process Telegram hub check without Telegram accounts or tokens."""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path


def _window(name, topic, token, database, incoming, output, stopped):
    import novacode_cli.memory.store as memory
    from novacode_cli.remote.telegram_hub import SharedTelegramPolling

    connection = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
    store = memory.DualModeStore(memory._StdlibSqliteStore(connection))
    memory.get_durable_store = lambda: store

    async def fetch(offset):
        output.put(("poll", name, offset))
        try:
            return [await asyncio.to_thread(incoming.get, True, 1)]
        except queue.Empty:
            return []

    async def run():
        client = SharedTelegramPolling(token, fetch)
        try:
            await client.reserve_session(7, name)
            await client.subscribe([(7, 0), (7, topic)])
            output.put(("ready", name))
            while not stopped.is_set():
                try:
                    events = await asyncio.wait_for(client.next_updates(), 0.5)
                    for event in events:
                        output.put(("message", name, event["update_id"]))
                except TimeoutError:
                    continue
        finally:
            await client.close()

    try:
        asyncio.run(run())
    finally:
        connection.close()


def verify():
    context = multiprocessing.get_context("spawn")
    incoming, output = context.Queue(), context.Queue()
    stop_a, stop_b = context.Event(), context.Event()
    token = "test-" + uuid.uuid4().hex
    observed = []

    def wait_for(expected):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            message = output.get(timeout=max(0.1, deadline - time.monotonic()))
            observed.append(message)
            if message == expected:
                return
        raise AssertionError(f"Missing {expected}; observed {observed}")

    with tempfile.TemporaryDirectory(prefix="nova-telegram-check-") as temporary:
        database = str(Path(temporary) / "store.db")
        a = context.Process(
            target=_window, args=("alpha", 55, token, database, incoming, output, stop_a)
        )
        b = context.Process(
            target=_window, args=("beta", 66, token, database, incoming, output, stop_b)
        )
        try:
            a.start()
            wait_for(("ready", "alpha"))
            b.start()
            wait_for(("ready", "beta"))
            incoming.put({"update_id": 1, "message": {"chat": {"id": 7}, "message_thread_id": 55}})
            wait_for(("message", "alpha", 1))
            incoming.put({"update_id": 2, "message": {"chat": {"id": 7}, "message_thread_id": 66}})
            wait_for(("message", "beta", 2))
            assert not any(item[:2] == ("poll", "beta") for item in observed)
            stop_a.set()
            a.join(10)
            assert a.exitcode == 0
            incoming.put({"update_id": 3, "message": {"chat": {"id": 7}, "message_thread_id": 66}})
            wait_for(("message", "beta", 3))
            assert any(item == ("poll", "beta", 3) for item in observed), observed
            assert not any(
                item in (("message", "alpha", 2), ("message", "beta", 1)) for item in observed
            )
            stop_b.set()
            b.join(10)
            assert b.exitcode == 0
        finally:
            stop_a.set()
            stop_b.set()
            for process in (a, b):
                if process.pid and process.is_alive():
                    process.terminate()
                    process.join(5)
            for channel in (incoming, output):
                channel.close()
                channel.join_thread()
    print("PASS: two OS processes, one poller, isolated topics, durable-offset handoff")


if __name__ == "__main__":
    verify()
