"""Guarded host bindings for the installed conversation-persistent QuickJS REPL."""
# ruff: noqa: INP001, ANN401, EM101, TRY003

from __future__ import annotations

import asyncio
import contextvars
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool
from langchain_quickjs._repl import _Registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

BOOTSTRAP = """
globalThis.__nova_mcp = tools.novaDispatch;
globalThis.tools = Object.freeze({
  list: (options = {}) => __nova_mcp({raw:JSON.stringify({
    ...options, namespace:'tools', action:'list'
  })}).then(JSON.parse),
  describe: (tool_name) => __nova_mcp({raw:JSON.stringify({
    namespace:'tools', action:'describe', tool_name
  })}).then(JSON.parse),
  call: (tool_name, args = {}) => __nova_mcp({raw:JSON.stringify({
    namespace:'tools', action:'call', tool_name, arguments:args
  })}).then(JSON.parse)
});
globalThis.mcp = Object.freeze({
  list: (options = {}) => __nova_mcp({raw:JSON.stringify({
    ...options, action:'list'
  })}).then(JSON.parse),
  describe: (server_name, tool_name) => __nova_mcp({raw:JSON.stringify({
    action:'describe', server_name, tool_name
  })}).then(JSON.parse),
  call: (server_name, tool_name, args = {}) => __nova_mcp({raw:JSON.stringify({
    action:'call', server_name, tool_name, arguments:args
  })}).then(JSON.parse)
});
globalThis.task = (options) => __nova_mcp({raw:JSON.stringify({
  ...options, namespace:'task'
})}).then(JSON.parse);
undefined;
"""


class ExecutionDeadline:
    """Exclude overlapping human waits from the active execution deadline."""

    def __init__(self, timeout: asyncio.Timeout) -> None:
        """Track the enclosing asyncio timeout and overlapping waits."""
        self.timeout = timeout
        self.waiters = 0
        self.remaining = 0.0

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Suspend the wall clock while at least one approval is pending."""
        if not self.waiters:
            when = self.timeout.when()
            if when is None:
                raise RuntimeError("Missing Code Mode execution deadline.")
            self.remaining = max(0, when - asyncio.get_running_loop().time())
            self.timeout.reschedule(None)
        self.waiters += 1
        try:
            yield
        finally:
            self.waiters -= 1
            if not self.waiters and not self.timeout.expired():
                self.timeout.reschedule(asyncio.get_running_loop().time() + self.remaining)


execution_deadline: contextvars.ContextVar[ExecutionDeadline | None] = contextvars.ContextVar(
    "code_execution_deadline", default=None
)


async def run_script(
    code: str,
    dispatch: Callable[[str], Awaitable[str]],
    *,
    cpu_timeout: float,
    wall_timeout: float,
    memory_limit: int,
    repl: Any = None,
    runtime: Any = None,
    expression: bool = False,
) -> tuple[Any, str | None]:
    """Install only a guarded proxy, never raw native or subagent tools."""
    registry = None
    if repl is None:
        registry = _Registry(
            memory_limit=memory_limit,
            timeout=cpu_timeout,
            capture_console=True,
            max_stdout_chars=12000,
            max_ptc_calls=None,
            subagents_enabled=False,
        )
        repl = registry.get("call")
    active = True
    pending: set[asyncio.Task] = set()
    parent = contextvars.copy_context()

    async def guarded(raw: str) -> str:
        if not active:
            raise ValueError("Code Mode invocation has ended.")

        async def invoke() -> str:
            return await dispatch(raw)

        task = asyncio.create_task(invoke(), context=parent.copy())
        pending.add(task)
        try:
            return await task
        finally:
            pending.discard(task)

    proxy = StructuredTool.from_function(
        name="nova_dispatch", description="Guarded Nova host dispatch.", coroutine=guarded
    )
    try:
        async with asyncio.timeout(wall_timeout) as timeout:
            token = execution_deadline.set(ExecutionDeadline(timeout))
            parent = contextvars.copy_context()
            try:
                await asyncio.to_thread(repl.install_tools, [proxy])
                setup = await repl.eval_async(BOOTSTRAP)
                if setup.error_type:
                    return None, setup.error_message
                source = (
                    code
                    if expression
                    else ("JSON.stringify(await (async () => {\n" + code + "\n})())")
                )
                outcome = await repl.eval_async(
                    source, outer_runtime=runtime, outer_loop=asyncio.get_running_loop()
                )
                if outcome.error_type:
                    return None, f"{outcome.error_type}: {outcome.error_message}"
                result = outcome.result
                if not expression:
                    result = None if result == "undefined" else json.loads(result)
                if outcome.stdout:
                    result = {"value": result, "stdout": outcome.stdout[:12000]}
                return result, None
            finally:
                execution_deadline.reset(token)
    finally:
        active = False
        for task in tuple(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if registry is not None:
            await registry.aevict("call")
