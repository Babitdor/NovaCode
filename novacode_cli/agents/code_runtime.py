"""Guarded host bindings for the installed conversation-persistent QuickJS REPL."""
# ruff: noqa: INP001, ANN401, EM101, TRY003

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool
from langchain_quickjs._repl import _Registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from langchain_quickjs._repl import _Slot
    from quickjs_rs.handle import Handle

logger = logging.getLogger(__name__)

HOST_CANCEL_GRACE = 0.5
REPL_START_TIMEOUT = 30

BOOTSTRAP = """
globalThis.tools = Object.freeze({
  search: (options = {}) => __nova_mcp({raw:JSON.stringify({
    limit:5, ...options, namespace:'tools', action:'search'
  })}).then(JSON.parse),
  list: (options = {}) => __nova_mcp({raw:JSON.stringify({
    ...options, namespace:'tools', action:'list'
  })}).then(JSON.parse),
  describe: (tool_name) => __nova_mcp({raw:JSON.stringify({
    namespace:'tools', action:'describe', tool_name
  })}).then(JSON.parse),
  call: (tool_name, args = {}) => __nova_mcp({raw:JSON.stringify({
    namespace:'tools', action:'call', tool_name, arguments:args
  })}).then(JSON.parse),
  checkpoint: (name, value) => __nova_mcp({raw:JSON.stringify({
    namespace:'checkpoint', name, value
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


def _close_unused_slot(registry: _Registry, slot: _Slot) -> None:
    try:
        registry._close_slot(slot)
    except Exception:  # noqa: BLE001 - Cleanup runs outside the turn.
        logger.warning("QuickJS discarded-slot cleanup failed", exc_info=True)


async def open_repl(registry: _Registry, thread: str) -> Any:
    """Publish a fresh worker only after its cancellable startup succeeds."""
    existing = registry.get_if_exists(thread)
    if existing is not None:
        return existing
    loop = asyncio.get_running_loop()
    # The upstream builder only constructs a slot. Inserting it separately keeps
    # the registry lock free and prevents a cancelled startup publishing late.
    startup = loop.run_in_executor(None, registry._build_slot_locked, thread)

    def discard(finished: asyncio.Future[_Slot]) -> None:
        if not finished.cancelled() and finished.exception() is None:
            loop.run_in_executor(None, _close_unused_slot, registry, finished.result())

    timeout = asyncio.timeout(REPL_START_TIMEOUT)
    try:
        async with timeout:
            slot = await asyncio.shield(startup)
    except asyncio.CancelledError:
        startup.add_done_callback(discard)
        raise
    except TimeoutError as exc:
        if not timeout.expired():
            raise
        startup.add_done_callback(discard)
        message = f"QuickJS startup exceeded its {REPL_START_TIMEOUT:g}s timeout."
        raise TimeoutError(message) from exc
    with registry._lock:
        current = registry._slots.setdefault(thread, slot)
    if current is not slot:
        loop.run_in_executor(None, _close_unused_slot, registry, slot)
    return current.repl


async def _eval_snippet(
    repl: Any,
    source: str,
    runtime: Any,
    outer_loop: asyncio.AbstractEventLoop,
    proxy: StructuredTool,
) -> tuple[Any, str | None]:
    """Cancel orphaned calls on settlement before QuickJS joins its TaskGroup."""
    await repl._ainstall_tools([proxy])
    symbol = json.dumps(repl._bridge_symbols["novaDispatch"])
    setup = await repl._aeval_async(f"globalThis.__nova_mcp = globalThis[{symbol}];{BOOTSTRAP}")
    if setup.error_type:
        return None, setup.error_message
    ctx = repl._require_ctx()
    # Script-mode QuickJS first resolves an envelope whose value can still be a Promise.
    adopt = ctx.eval_handle("(async envelope => ({value: await (await envelope).value}))")
    drive = ctx._run_inside_task_group

    def cancel_orphans() -> None:
        if not ctx._pending_tasks:
            return
        # Drain cancellation reactions on the owning worker. A settled snippet
        # must never dispatch another tool through a later snippet's bridge.
        ctx._engine_ctx.set_async_host_dispatcher(lambda *_: -1)
        try:
            ctx._reject_pending_with_cancellation()
            for task in ctx._pending_tasks.values():
                task.cancel()
            ctx._pending_tasks.clear()
            ctx._engine_ctx.run_pending_jobs()
        finally:
            ctx._engine_ctx.set_async_host_dispatcher(ctx._dispatch_async_host_call)

    async def drive_until_settled(get_handle: Callable[[], Handle], deadline: float) -> Handle:
        def tracked_handle() -> Handle:
            original = get_handle()
            try:
                handle = adopt.call(original)
            finally:
                original.dispose()
            dispose = handle.dispose

            def settle() -> None:
                del handle.__dict__["dispose"]
                fulfilled = ctx._engine_ctx.promise_state(handle._require_live()) == 1
                dispose()
                if fulfilled:
                    cancel_orphans()

            handle.__dict__["dispose"] = settle
            return handle

        return await drive(tracked_handle, deadline)

    # The pinned engine disposes the top-level promise as soon as it settles,
    # before joining pending host calls. Keep the hook local to this eval.
    ctx._run_inside_task_group = drive_until_settled
    try:
        outcome = await repl._aeval_async(source, outer_runtime=runtime, outer_loop=outer_loop)
        return outcome, None
    finally:
        del ctx._run_inside_task_group
        adopt.dispose()


_background_closures: set[asyncio.Task[None]] = set()


async def _close_after_eval(
    registry: _Registry, slot: _Slot, evaluation: asyncio.Future[Any]
) -> None:
    await asyncio.gather(evaluation, return_exceptions=True)
    await asyncio.to_thread(_close_unused_slot, registry, slot)


async def _run_worker(
    registry: _Registry,
    thread: str,
    repl: Any,
    source: str,
    runtime: Any,
    proxy: StructuredTool,
) -> tuple[Any, str | None]:
    worker_task: asyncio.Task[Any] | None = None
    cancel_requested = False
    outer_loop = asyncio.get_running_loop()

    async def evaluate() -> tuple[Any, str | None]:
        nonlocal worker_task
        worker_task = asyncio.current_task()
        if cancel_requested:
            raise asyncio.CancelledError
        return await _eval_snippet(repl, source, runtime, outer_loop, proxy)

    def cancel_evaluation() -> None:
        nonlocal cancel_requested
        cancel_requested = True
        if worker_task is not None:
            worker_task.cancel()

    evaluation = repl._worker.run_async(evaluate())
    try:
        return await asyncio.shield(evaluation)
    except asyncio.CancelledError:
        repl._worker._loop.call_soon_threadsafe(cancel_evaluation)
        with registry._lock:
            slot = registry._slots.pop(thread, None)
        if slot is not None:
            close = asyncio.create_task(_close_after_eval(registry, slot, evaluation))
            _background_closures.add(close)
            close.add_done_callback(_background_closures.discard)
        raise


async def _cancel_host_calls(pending: set[asyncio.Task[str]]) -> None:
    if not pending:
        return
    for task in pending:
        if not task.cancelling():
            task.cancel()
    # A transport may shield teardown or swallow cancellation.
    _, lingering = await asyncio.wait(pending, timeout=HOST_CANCEL_GRACE)
    if lingering:
        logger.warning(
            "Code Mode ended while %d nested tool call(s) were still cleaning up",
            len(lingering),
        )


@asynccontextmanager
async def _script_deadline(seconds: float) -> AsyncIterator[None]:
    timeout = asyncio.timeout(seconds)
    try:
        async with timeout:
            token = execution_deadline.set(ExecutionDeadline(timeout))
            try:
                yield
            finally:
                execution_deadline.reset(token)
    except TimeoutError as exc:
        if not timeout.expired():
            raise
        message = (
            f"Script exceeded its {seconds:g}s Code Mode wall timeout. "
            "Unsaved JavaScript state was cleared."
        )
        raise TimeoutError(message) from exc


async def run_script(
    code: str,
    dispatch: Callable[[str], Awaitable[str]],
    *,
    cpu_timeout: float,
    wall_timeout: float,
    memory_limit: int,
    registry: _Registry | None = None,
    thread: str = "call",
    runtime: Any = None,
    expression: bool = False,
) -> tuple[Any, str | None]:
    """Install only a guarded proxy, never raw native or subagent tools."""
    owns_registry = registry is None
    if registry is None:
        registry = _Registry(
            memory_limit=memory_limit,
            timeout=cpu_timeout,
            capture_console=True,
            max_stdout_chars=12000,
            max_ptc_calls=None,
            subagents_enabled=False,
        )
    active = True
    pending: set[asyncio.Task[str]] = set()
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
        repl = await open_repl(registry, thread)
        async with _script_deadline(wall_timeout):
            parent = contextvars.copy_context()
            source = (
                code if expression else ("JSON.stringify(await (async () => {\n" + code + "\n})())")
            )
            outcome, error = await _run_worker(registry, thread, repl, source, runtime, proxy)
            if error is not None:
                return None, error
            if outcome.error_type:
                if outcome.error_type == "Timeout":
                    return None, f"QuickJS exceeded its {cpu_timeout:g}s interpreter timeout."
                return None, f"{outcome.error_type}: {outcome.error_message}"
            result = outcome.result
            if not expression:
                result = None if result == "undefined" else json.loads(result)
            if outcome.stdout:
                result = {"value": result, "stdout": outcome.stdout[:12000]}
            return result, None
    finally:
        active = False
        try:
            await _cancel_host_calls(pending)
        finally:
            if owns_registry:
                await registry.aevict(thread)
