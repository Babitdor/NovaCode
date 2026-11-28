"""The eval sandbox: persistent-scope traps, timers, and recovery hints.

A live session failed with `SyntaxError: redeclaration of 'r'` (top-level
declarations persist between evals in one thread) and
`ReferenceError: setTimeout is not defined` (the sandbox has no DOM timers).
The sandbox now ships a bounded `sleep` host call and a `setTimeout` shim, and
the recovery hints name the actual trap.
"""

from __future__ import annotations

import asyncio
import json

from novacode_cli.agents.code_mode import _recovery_hint
from novacode_cli.agents.code_runtime import _Registry, run_script


def _registry() -> _Registry:
    return _Registry(
        memory_limit=8 * 1024 * 1024,
        timeout=5,
        capture_console=True,
        max_stdout_chars=12000,
        subagents_enabled=False,
    )


def _dispatch():
    async def dispatch(raw: str) -> str:
        data = json.loads(raw)
        if data.get("namespace") == "sleep":
            ms = min(max(int(data.get("ms") or 0), 0), 10_000)
            await asyncio.sleep(ms / 1000)
            return json.dumps({"slept_ms": ms})
        return "null"

    return dispatch


async def _eval_shared(
    code: str, registry: _Registry, thread: str = "shared", *, wall_timeout: float = 10
):
    return await run_script(
        code,
        _dispatch(),
        cpu_timeout=5,
        wall_timeout=wall_timeout,
        memory_limit=8 * 1024 * 1024,
        registry=registry,
        thread=thread,
        expression=True,
    )


async def test_eval_redeclaration_is_a_parse_error():
    """The trace's exact failure: top-level `let`/`const` bindings persist
    between evals in one thread, so re-declaring the same name in a later eval
    throws `redeclaration of 'r'`. The escape hatch is block scoping."""
    registry = _registry()
    first, error = await _eval_shared("let r = 1; r", registry)
    assert error is None and first == "1"
    second, error = await _eval_shared("let r = 2; r", registry)
    assert error is not None and "redeclaration of 'r'" in error
    third, error = await _eval_shared("{ let r = 2; r }", registry)
    assert error is None and third == "2", "block scoping is the escape hatch"


async def test_sleep_is_available_and_bounded():
    result, error = await _eval_shared("JSON.stringify(await sleep(20))", _registry())
    assert error is None
    assert result == '{"slept_ms":20}'


async def test_settimeout_shim_resolves_after_the_wait():
    result, error = await _eval_shared(
        "JSON.stringify(await setTimeout((v) => ['done', v], 15))",
        _registry(),
    )
    assert error is None
    assert result == '["done",{"slept_ms":15}]'


async def test_sleep_is_capped_host_side():
    # The host cap keeps a runaway poll inside the snippet's wall budget.
    result, error = await _eval_shared(
        "JSON.stringify(await sleep(999999))", _registry(), wall_timeout=15
    )
    assert error is None
    assert result == '{"slept_ms":10000}'


def test_recovery_hints_name_the_actual_trap():
    hint = _recovery_hint(ValueError("SyntaxError: redeclaration of 'r'"), [], omitted=False)
    assert "persist between evals" in hint
    assert "{ ... }" in hint

    hint = _recovery_hint(
        ValueError("ReferenceError: setTimeout is not defined"), [], omitted=False
    )
    assert "await sleep(ms)" in hint
    assert "setTimeout" in hint
