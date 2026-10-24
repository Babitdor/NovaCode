"""Isolated QuickJS execution with bounded calls back to the owning agent loop."""
# ruff: noqa: INP001, BLE001 -- namespace package; failures converted on their owner thread.

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
from typing import TYPE_CHECKING, Any

from quickjs_rs import Runtime, ThreadWorker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

BOOTSTRAP = """
Object.defineProperty(globalThis, 'tools', {value: Object.freeze({
  list: (options = {}) => __nova_mcp(JSON.stringify({
    ...options, namespace:'tools', action:'list'
  })).then(JSON.parse),
  describe: (tool_name) => __nova_mcp(JSON.stringify({
    namespace:'tools', action:'describe', tool_name
  })).then(JSON.parse),
  call: (tool_name, args = {}) => __nova_mcp(JSON.stringify({
    namespace:'tools', action:'call', tool_name, arguments:args
  })).then(JSON.parse)
}), writable:false, configurable:false});
Object.defineProperty(globalThis, 'mcp', {value: Object.freeze({
  list: (options = {}) => __nova_mcp(JSON.stringify({...options, action:'list'})).then(JSON.parse),
  describe: (server_name, tool_name) => __nova_mcp(JSON.stringify({
    action:'describe', server_name, tool_name
  })).then(JSON.parse),
  call: (server_name, tool_name, args = {}) => __nova_mcp(JSON.stringify({
    action:'call', server_name, tool_name, arguments:args
  })).then(JSON.parse)
}), writable:false, configurable:false});
undefined;
"""


async def run_script(
    code: str,
    dispatch: Callable[[str], Awaitable[str]],
    *,
    cpu_timeout: float,
    wall_timeout: float,
    memory_limit: int,
) -> tuple[Any, str | None]:
    """Run fresh JS state; preserve parent callback/context across worker hops."""
    loop = asyncio.get_running_loop()
    parent = contextvars.copy_context()
    pending: set[asyncio.Task[str]] = set()
    active = True
    worker = ThreadWorker(name="nova-mcp-code")

    async def bridge(raw: str) -> str:
        future: concurrent.futures.Future[str] = concurrent.futures.Future()

        def start() -> None:
            if not active or future.cancelled():
                future.cancel()
                return

            async def invoke() -> str:
                return await dispatch(raw)

            task = loop.create_task(invoke())
            pending.add(task)

            def done(completed: asyncio.Task[str]) -> None:
                pending.discard(completed)
                if completed.cancelled():
                    future.cancel()
                elif not future.done():
                    error = completed.exception()
                    if error is None:
                        future.set_result(completed.result())
                    else:
                        future.set_exception(error)
                else:
                    completed.exception()

            task.add_done_callback(done)
            future.add_done_callback(
                lambda finished: (
                    loop.call_soon_threadsafe(task.cancel) if finished.cancelled() else None
                )
            )

        loop.call_soon_threadsafe(start, context=parent.copy())
        return await asyncio.wrap_future(future)

    async def evaluate() -> tuple[Any, str | None]:
        # All Rust handles, including exception tracebacks, stay on their owner thread.
        with (
            Runtime(memory_limit=memory_limit) as runtime,
            runtime.new_context(timeout=cpu_timeout) as context,
        ):
            context.register("__nova_mcp", bridge, is_async=True)
            context.eval(BOOTSTRAP)
            try:
                result = await context.eval_async(
                    "await (async () => {\n" + code + "\n})()",
                    timeout=cpu_timeout,
                )
            except Exception as exc:
                # Never move a traceback retaining !Send QuickJS handles across threads.
                message = f"{type(exc).__name__}: {exc}"
                exc.__traceback__ = None
                exc.__context__ = None
                exc.__cause__ = None
                return None, message[:4000]
            else:
                return result, None

    try:
        return await asyncio.wait_for(worker.run_async(evaluate()), wall_timeout)
    finally:
        active = False
        for task in tuple(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*tuple(pending), return_exceptions=True)
        await asyncio.to_thread(worker.close)
