"""Smoke-test the installed Code Mode bridge without credentials or model calls."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.code_mode import CodeModeMiddleware
from novacode_cli.agents.code_runtime import run_script
from novacode_cli.backends.filesystem import OptimizedFilesystemBackend
from novacode_cli.tracking.loop_guard import LoopGuardMiddleware

if TYPE_CHECKING:
    from typing import Any

    from langchain.agents.middleware.types import AgentMiddleware, AgentState


class _OfflineModel(FakeMessagesListChatModel):
    """Return scripted tool calls without contacting a model endpoint."""

    def bind_tools(self, *_args: object, **_kwargs: object) -> _OfflineModel:
        """Retain the scripted responses when the graph binds tools."""
        return self


async def _verify_read_edit_round_trip() -> None:
    """Exercise the installed renderer and backend without touching user files."""
    with TemporaryDirectory(prefix="nova-installed-code-data-") as directory:
        root = Path(directory)
        original = "@@ lines 1-1 of 1 @@\n[note: literal source]\n"
        path = root / "guide.md"
        path.write_text(original, encoding="utf-8")
        code = (
            "var first = await tools.call('read_file', {file_path:'/guide.md'}); "
            "var again = await tools.call('read_file', {file_path:'/guide.md'}); "
            "await tools.call('edit_file', {file_path:'/guide.md', "
            "old_string:again, new_string:again+'Updated'}); first===again;"
        )
        model = _OfflineModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "installed-read-edit",
                            "name": "eval",
                            "args": {"code": code},
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        mode = CodeModeMiddleware(auto_approve=True)
        # Graph construction merges the extended state schemas of these middleware.
        middleware = cast(
            "list[AgentMiddleware[AgentState[Any], None, Any]]",
            [
                mode,
                LoopGuardMiddleware(),
                FilesystemMiddleware(
                    backend=OptimizedFilesystemBackend(root_dir=root, virtual_mode=True)
                ),
            ],
        )
        graph = create_agent(model, middleware=middleware)
        try:
            # Permissions are tested separately; this smoke owns only fixed temp-file operations.
            with (
                patch(
                    "novacode_cli.agents.code_mode.NovaConfig.get_code_mode_enabled",
                    return_value=True,
                ),
                patch(
                    "novacode_cli.agents.code_mode.get_policy",
                    return_value=SimpleNamespace(tool_default=lambda _name: "allow"),
                ),
                patch.object(mode, "_authorize", new=AsyncMock(return_value=None)),
            ):
                result = await graph.ainvoke({"messages": [HumanMessage(content="Read and edit")]})
            message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
            assert message.status == "success", message.content
            assert isinstance(message.content, str)
            assert json.loads(message.content)["result"] == "true"
            assert path.read_text(encoding="utf-8") == original + "Updated"
        finally:
            await mode._registry.aevict(mode._fallback_thread_id)


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
    await _verify_read_edit_round_trip()


if __name__ == "__main__":
    asyncio.run(verify())
    print(f"Installed Code Mode passed (langchain-quickjs {version('langchain-quickjs')}).")
