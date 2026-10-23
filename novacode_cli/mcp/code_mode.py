"""Programmatic MCP composition through Nova's existing tool middleware."""
# ruff: noqa: ANN401, BLE001, EM101, EM102, TRY003, TRY301
# JSON payloads are heterogeneous; the tool boundary reports arbitrary server failures.

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from novacode_cli.agents.strict_tools import _validator
from novacode_cli.mcp.generic import MCPInput, build_mcp_tool, resolve_target
from novacode_cli.security.policy import get_policy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.messages.tool import ToolCall

MAX_ARGUMENT_CHARS = 100000
MAX_INTERMEDIATE_CHARS = 1_000_000
MAX_OUTPUT_CHARS = 12000


class CodeInput(BaseModel):
    """A bounded JavaScript async function body."""

    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=40000)


class MCPCodeModeMiddleware(AgentMiddleware):
    """Must precede other tool middleware so nested requests retain their guards."""

    def __init__(
        self,
        mcp: Any,
        *,
        auto_approve: bool = False,
        concurrency: int = 4,
        max_calls: int = 32,
        cpu_timeout: float = 2,
        wall_timeout: float = 60,
        call_timeout: float = 30,
        memory_limit: int = 64 * 1024 * 1024,
    ) -> None:
        """Use the current graph's filtered MCP inventory and approval setting."""
        super().__init__()
        if min(concurrency, max_calls, cpu_timeout, wall_timeout, call_timeout, memory_limit) <= 0:
            raise ValueError("Code Mode limits must be positive.")
        self._mcp = mcp
        self._catalog = build_mcp_tool(mcp)
        self._auto_approve = auto_approve
        self._concurrency = concurrency
        self._max_calls = max_calls
        self._cpu_timeout = cpu_timeout
        self._wall_timeout = wall_timeout
        self._call_timeout = call_timeout
        self._memory_limit = memory_limit

        def unavailable(code: str) -> str:  # noqa: ARG001 -- public tool schema.
            return "MCPCode requires its owning agent middleware."

        self.tools = [
            StructuredTool.from_function(
                unavailable,
                name="MCPCode",
                args_schema=CodeInput,
                description=(
                    "Run JavaScript to discover, call and process configured MCP tools. "
                    "code is an async function body: return the final small result. "
                    "await mcp.list({server_name?, offset?, limit?}); "
                    "await mcp.describe(server_name, tool_name) returns the original schema; "
                    "await mcp.call(server_name, tool_name, arguments) returns structured data. "
                    "Use exact names/schema from discovery. Supports loops and Promise.all. "
                    "No filesystem, network, imports or persistent state. Nested calls retain "
                    "permissions; ask-tier calls require the normal MCPTool approval path. "
                    "Do not retry writes blindly after partial failures or timeouts."
                ),
            )
        ]

    @staticmethod
    def _message(request: ToolCallRequest, content: str, *, error: bool = True) -> ToolMessage:
        return ToolMessage(
            content=content,
            name="MCPCode",
            tool_call_id=request.tool_call["id"],
            status="error" if error else "success",
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """Fail closed in sync graphs; Nova's normal agent execution is async."""
        if request.tool_call["name"] != "MCPCode":
            return handler(request)
        return self._message(request, "MCPCode requires asynchronous agent execution.")

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Route each nested call through the remaining middleware and ToolNode."""
        if request.tool_call["name"] != "MCPCode":
            return await handler(request)
        if get_policy().tool_default("MCPCode") == "deny":
            return self._message(request, "MCPCode denied by approval policy.")
        if get_policy().tool_default("MCPCode") == "ask" and not self._auto_approve:
            return self._message(
                request, "MCPCode requires approval; configure its policy before use."
            )

        async def execute_code(code: str) -> ToolMessage:
            params = CodeInput(code=code)
            return await self._execute(request, handler, params.code)

        # Execute inside ToolNode, so tool callbacks and downstream wrappers see
        # the actual duration, result and failures, not a preliminary probe.
        live_tool = self.tools[0].model_copy(update={"coroutine": execute_code})
        try:
            return await handler(request.override(tool=live_tool))
        except Exception as exc:
            return self._message(request, f"MCPCode failed: {type(exc).__name__}: {exc}"[:4000])

    async def _execute(self, request: ToolCallRequest, handler: Callable, code: str) -> ToolMessage:
        from novacode_cli.mcp.code_runtime import run_script

        calls = failures = 0
        semaphore = asyncio.Semaphore(self._concurrency)

        async def dispatch(raw: str) -> str:
            nonlocal calls, failures
            calls += 1
            try:
                if calls > self._max_calls:
                    raise ValueError("MCPCode call budget exceeded.")
                if not isinstance(raw, str) or len(raw) > MAX_ARGUMENT_CHARS:
                    raise ValueError("MCPCode arguments exceed the size limit.")
                params = MCPInput.model_validate_json(raw, strict=True)
                if get_policy().tool_default("MCPTool") == "deny":
                    raise ValueError("MCPTool denied by approval policy.")
                async with semaphore:
                    if params.action != "call":
                        value = self._catalog.invoke(params.model_dump())
                    else:
                        value = await self._call(request, handler, params)
                    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                    if len(encoded) > MAX_INTERMEDIATE_CHARS:
                        raise ValueError(
                            "MCP result exceeds 1000000 characters; request fewer records."
                        )
                    return encoded
            except Exception:
                failures += 1
                raise

        try:
            result, error = await run_script(
                code,
                dispatch,
                cpu_timeout=self._cpu_timeout,
                wall_timeout=self._wall_timeout,
                memory_limit=self._memory_limit,
            )
            if error is not None:
                raise ValueError(error)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            truncated = len(encoded) > MAX_OUTPUT_CHARS
            body = {
                "result": encoded[:MAX_OUTPUT_CHARS] if truncated else result,
                "truncated": truncated,
                "calls": calls,
                "failed_calls": failures,
            }
            return self._message(request, json.dumps(body, ensure_ascii=False), error=False)
        except Exception as exc:
            return self._message(
                request,
                json.dumps(
                    {
                        "error": f"{type(exc).__name__}: {exc}"[:4000],
                        "calls": calls,
                        "failed_calls": failures,
                    }
                ),
            )

    async def _call(self, request: ToolCallRequest, handler: Callable, params: MCPInput) -> Any:
        target = resolve_target(self._mcp, params)
        if target is None:
            raise ValueError("Unknown or disabled MCP tool; use mcp.list().")
        schema = target.tool_call_schema
        if not isinstance(schema, dict):
            schema = schema.model_json_schema()
        try:
            _validator(json.dumps(schema, sort_keys=True), close_named=True).validate(
                params.arguments
            )
        except Exception as exc:
            raise ValueError(f"Invalid tool arguments: {exc}") from exc
        await self._authorize(request, target.name, params.arguments)
        call_id = f"mcp-code-{uuid4().hex}"
        call: ToolCall = {
            "name": target.name,
            "args": params.arguments,
            "id": call_id,
            "type": "tool_call",
        }
        if request.runtime is None:
            raise ValueError("MCPCode requires its owning agent runtime.")
        runtime = replace(request.runtime, tool_call_id=call_id)
        nested = replace(request, tool=target, tool_call=call, runtime=runtime)
        deadline = asyncio.timeout(self._call_timeout)
        async with deadline:
            result = await handler(nested)
        # Some MCP adapters convert cancellation into a textual tool result.
        if deadline.expired():
            raise TimeoutError("MCP tool call timeout.")
        if not isinstance(result, ToolMessage):
            raise TypeError("MCP tool returned unsupported state mutation.")
        if result.status == "error":
            raise ValueError(str(result.content))
        value = self._result_value(result)
        if isinstance(value, str) and value.startswith("[MCP error]"):
            raise ValueError(value)
        return value

    async def _authorize(self, request: ToolCallRequest, name: str, arguments: dict) -> None:
        """Reuse session permissions and optional approval classifiers."""
        from novacode_cli.ui.hitl_approval import evaluate_tool_actions

        user_request = next(
            (
                m.content
                for m in reversed(request.state.get("messages", []))
                if getattr(m, "type", "") == "human" and isinstance(m.content, str)
            ),
            "",
        )
        decisions = await asyncio.to_thread(
            evaluate_tool_actions,
            {"action_requests": [{"name": name, "args": arguments}]},
            SimpleNamespace(auto_approve=self._auto_approve),
            user_request=user_request[:4000],
        )
        decision = decisions[0] if len(decisions) == 1 else None
        if decision is None:
            raise ValueError(f"{name} requires approval. Use the normal MCPTool call path.")
        if decision.get("type") != "approve":
            raise ValueError(f"{name} denied: {decision.get('reason', 'approval rejected')}")
        if get_policy().tool_default("MCPTool") == "deny":
            raise ValueError("MCPTool denied by approval policy.")

    @staticmethod
    def _result_value(result: ToolMessage) -> Any:
        """Keep structured MCP values inside the script."""
        artifact = result.artifact
        if isinstance(artifact, dict) and "structured_content" in artifact:
            return artifact["structured_content"]
        value = result.content
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        if (
            isinstance(value, list)
            and len(value) == 1
            and isinstance(value[0], dict)
            and value[0].get("type") == "text"
        ):
            try:
                return json.loads(value[0]["text"])
            except ValueError:
                return value[0]["text"]
        return value
