"""Smoke-test the installed Code Mode bridge without credentials or model calls."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import version

from novacode_cli.agents.code_mode import CodeModeMiddleware
from novacode_cli.agents.code_runtime import run_script


async def verify() -> None:
    """Check runtime construction, guarded host dispatch and persistent state."""
    middleware = CodeModeMiddleware()
    assert {tool.name for tool in middleware.tools} == {"eval", "MCPCode"}
    thread = middleware._fallback_thread_id
    repl = middleware._registry.get(thread)

    async def dispatch(raw: str) -> str:
        payload = json.loads(raw)
        assert payload == {
            "namespace": "tools",
            "action": "call",
            "tool_name": "echo",
            "arguments": {"value": 42},
        }
        return json.dumps(payload["arguments"]["value"])

    try:
        for code, expected in (
            ("globalThis.installedValue = 40; return installedValue;", 40),
            ("return await tools.call('echo', {value: installedValue + 2});", 42),
        ):
            result, error = await run_script(
                code,
                dispatch,
                repl=repl,
                cpu_timeout=2,
                wall_timeout=30,
                memory_limit=64 * 1024 * 1024,
            )
            assert error is None, error
            assert result == expected, result
    finally:
        await middleware._registry.aevict(thread)


if __name__ == "__main__":
    asyncio.run(verify())
    print(f"Installed Code Mode passed (langchain-quickjs {version('langchain-quickjs')}).")
