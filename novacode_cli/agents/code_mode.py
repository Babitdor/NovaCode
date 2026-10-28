"""Programmatic tool composition through the owning graph's tool middleware."""
# ruff: noqa: INP001, ANN401, BLE001, EM101, EM102, TRY003, TRY301
# JSON payloads are heterogeneous; the tool boundary reports arbitrary server failures.

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from novacode_cli.agents.strict_tools import _validator
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.mcp.generic import MCPInput, build_mcp_tool, resolve_target
from novacode_cli.security.policy import get_policy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.messages.tool import ToolCall

MAX_ARGUMENT_CHARS = 100000
MAX_INTERMEDIATE_CHARS = 1_000_000
MAX_OUTPUT_CHARS = 12000

# These require their own graph step, user interaction, or interpreter lifecycle.
DIRECT_ONLY_TOOLS = frozenset(
    {
        "MCPCode",
        "MCPTool",
        "eval",
        "task",
        "write_todos",
        "handoff_save",  # Owns foreground context; keep on the direct graph tool path.
        "wait_for_task",
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
        "EnterWorktreeTool",
        "ExitWorktreeTool",
        "computer_screenshot",  # Image results need the direct multimodal tool path.
        "computer_action",
    }
)


class NativeInput(BaseModel):
    """Discovery and calls over the owning graph's ordinary tool inventory."""

    model_config = ConfigDict(extra="forbid", strict=True)
    namespace: Literal["tools"]
    action: Literal["list", "describe", "call"]
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


def _native_tools(request: ToolCallRequest) -> dict[str, BaseTool]:
    """Read capabilities from this invocation; never retain another graph's tools."""
    if request.runtime is None:
        raise ValueError("Code Mode requires its owning agent runtime.")
    available = {}
    for target in request.runtime.tools:
        if target.name in DIRECT_ONLY_TOOLS or (target.metadata or {}).get("nova_mcp_server"):
            continue
        # Most control tools declare Command returns. read_file also declares it
        # for media; ordinary text reads are supported, media falls back to direct.
        functions = (getattr(target, "func", None), getattr(target, "coroutine", None))
        if not any(functions):
            functions = (target._run, target._arun)
        if target.name != "read_file" and any(
            re.search(r"\bCommand\b", str(inspect.signature(fn).return_annotation))
            for fn in functions
            if fn is not None
        ):
            continue
        available[target.name] = target
    return available


class CodeInput(BaseModel):
    """A bounded JavaScript async function body."""

    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=40000)


class CodeModeMiddleware(AgentMiddleware):
    """Must precede other tool middleware so nested requests retain their guards."""

    def __init__(
        self,
        mcp: Any = None,
        *,
        auto_approve: bool = False,
        concurrency: int = 4,
        max_calls: int = 32,
        cpu_timeout: float = 2,
        wall_timeout: float = 60,
        call_timeout: float = 30,
        memory_limit: int = 64 * 1024 * 1024,
    ) -> None:
        """Use the owning graph's tools, optional MCP inventory and approval setting."""
        super().__init__()
        if min(concurrency, max_calls, cpu_timeout, wall_timeout, call_timeout, memory_limit) <= 0:
            raise ValueError("Code Mode limits must be positive.")
        self._mcp = mcp
        self._catalog = build_mcp_tool(mcp) if mcp is not None else None
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
                    "Run JavaScript to discover, call and process Nova tools. "
                    "code is an async function body: return the final small result. "
                    "await tools.list({offset?, limit?}); await tools.describe(tool_name); "
                    "await tools.call(tool_name, arguments) returns structured data. "
                    "Only ordinary registered tools are available; graph controls, delegation "
                    "and recursive interpreters require direct calls. "
                    "await mcp.list({server_name?, offset?, limit?}); "
                    "await mcp.describe(server_name, tool_name) returns the original schema; "
                    "await mcp.call(server_name, tool_name, arguments) returns structured data. "
                    "Use exact names/schema from discovery. Supports loops and Promise.all. "
                    "No filesystem, network, imports or persistent state. Nested calls retain "
                    "permissions; calls needing user approval must use the direct tool path. "
                    "Text file reads are supported; media reads require direct read_file calls. "
                    "Do not retry writes blindly after partial failures or timeouts."
                ),
            )
        ]

    @staticmethod
    def _model_request(request: ModelRequest, *, enabled: bool) -> ModelRequest:
        if enabled:
            return request
        return request.override(
            tools=[
                t
                for t in request.tools
                if (t.get("name") if isinstance(t, dict) else t.name) != "MCPCode"
            ]
        )

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> Any:
        """Expose Code Mode only while the saved preference is enabled."""
        return handler(self._model_request(request, enabled=NovaConfig().get_code_mode_enabled()))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> Any:
        """Apply preference changes to existing graphs without rebuilding them."""
        enabled = await asyncio.to_thread(lambda: NovaConfig().get_code_mode_enabled())
        return await handler(self._model_request(request, enabled=enabled))

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
        if not await asyncio.to_thread(lambda: NovaConfig().get_code_mode_enabled()):
            return self._message(request, "MCP Code Mode is disabled.")
        if get_policy().tool_default("MCPCode") == "deny":
            return self._message(request, "MCPCode denied by approval policy.")
        if get_policy().tool_default("MCPCode") == "ask" and not self._auto_approval_enabled():
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
        from novacode_cli.agents.code_runtime import run_script

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
                payload = json.loads(raw)
                async with semaphore:
                    if isinstance(payload, dict) and payload.get("namespace") == "tools":
                        native = NativeInput.model_validate(payload)
                        value = await self._native(request, handler, native)
                    else:
                        params = MCPInput.model_validate(payload, strict=True)
                        if get_policy().tool_default("MCPTool") == "deny":
                            raise ValueError("MCPTool denied by approval policy.")
                        if self._catalog is None:
                            raise ValueError("No MCP tools configured. Use tools.list().")
                        if params.action != "call":
                            value = self._catalog.invoke(params.model_dump())
                        else:
                            value = await self._call(request, handler, params)
                    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                    if len(encoded) > MAX_INTERMEDIATE_CHARS:
                        raise ValueError(
                            "Tool result exceeds 1000000 characters; request fewer records."
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

    async def _native(
        self, request: ToolCallRequest, handler: Callable, params: NativeInput
    ) -> Any:
        available = _native_tools(request)
        if params.action == "list":
            entries = [
                {"name": t.name, "description": t.description[:300]} for t in available.values()
            ]
            return {
                "tools": entries[params.offset : params.offset + params.limit],
                "total": len(entries),
                "offset": params.offset,
            }
        target = available.get(params.tool_name)
        if target is None:
            raise ValueError(
                "Unknown or unsupported tool; use tools.list() or call the tool directly."
            )
        if params.action == "describe":
            return {"name": target.name, "schema": convert_to_openai_tool(target)["function"]}
        return await self._invoke(request, handler, target, params.arguments)

    async def _call(self, request: ToolCallRequest, handler: Callable, params: MCPInput) -> Any:
        target = resolve_target(self._mcp, params)
        if target is None:
            raise ValueError("Unknown or disabled MCP tool; use mcp.list().")
        return await self._invoke(request, handler, target, params.arguments, mcp=True)

    async def _invoke(
        self,
        request: ToolCallRequest,
        handler: Callable,
        target: BaseTool,
        arguments: dict,
        *,
        mcp: bool = False,
    ) -> Any:
        schema = convert_to_openai_tool(target)["function"]["parameters"]
        try:
            _validator(json.dumps(schema, sort_keys=True), close_named=True).validate(arguments)
        except Exception as exc:
            raise ValueError(f"Invalid tool arguments: {exc}") from exc
        await self._authorize(request, target.name, arguments)
        if mcp and get_policy().tool_default("MCPTool") == "deny":
            raise ValueError("MCPTool denied by approval policy.")
        call_id = f"mcp-code-{uuid4().hex}"
        call: ToolCall = {
            "name": target.name,
            "args": arguments,
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
            raise TypeError("Tool returned unsupported state mutation; call this tool directly.")
        if result.status == "error":
            raise ValueError(str(result.content))
        value = self._result_value(result)
        if isinstance(value, str) and value.startswith("[MCP error]"):
            raise ValueError(value)
        return value

    def _auto_approval_enabled(self) -> bool:
        from novacode_cli.config.plan_mode import active_plan_session

        session = active_plan_session.get()
        return bool(getattr(session, "auto_approve", self._auto_approve))

    async def _authorize(self, request: ToolCallRequest, name: str, arguments: dict) -> None:
        """Reuse session permissions and optional approval classifiers."""
        from novacode_cli.security.approval_context import request_user_text
        from novacode_cli.ui.hitl_approval import evaluate_tool_actions

        user_request = request_user_text(request)
        decisions = await asyncio.to_thread(
            evaluate_tool_actions,
            {"action_requests": [{"name": name, "args": arguments}]},
            SimpleNamespace(auto_approve=self._auto_approval_enabled()),
            user_request=user_request,
        )
        decision = decisions[0] if len(decisions) == 1 else None
        if decision is None:
            raise ValueError(
                f"{name} requires approval. Call the tool directly (MCPTool for MCP calls)."
            )
        if decision.get("type") != "approve":
            raise ValueError(f"{name} denied: {decision.get('reason', 'approval rejected')}")

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
