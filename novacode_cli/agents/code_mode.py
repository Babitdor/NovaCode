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

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_quickjs import CodeInterpreterMiddleware
from langchain_quickjs.middleware import _resolve_thread_id
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from novacode_cli.agents.strict_tools import _validator
from novacode_cli.agents.tool_search_variants import SEARCH_VARIANTS
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.mcp.generic import MCPInput, build_mcp_tool, resolve_target
from novacode_cli.security.policy import get_policy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
    from langchain_core.messages.tool import ToolCall

MAX_ARGUMENT_CHARS = 100000
MAX_INTERMEDIATE_CHARS = 1_000_000
MAX_OUTPUT_CHARS = 12000

CODE_MODE_GUIDANCE = """
## Code Mode
Use eval as your primary control surface for ordinary operations. Compose discovery,
native calls, MCP calls, filtering and bounded Promise.all fan-out in JavaScript.
Use tools.list/describe/call and mcp.list/describe/call; inspect schemas before calls.
Use task({description, subagentType, responseSchema?}) for configured subagents,
including explicit @agent requests. tools.describe('task') lists the current roster.
eval accepts statements and top-level await; its final expression is the selected result.
Variables persist within this conversation. Keep intermediate data in JavaScript;
return a compact synthesis or explicitly print selected output with console.log.
MCPCode is a compatibility async-function body using the same guarded runtime.
Human approvals pause the live script. Never retry completed side effects after failure.
Interaction, planning, workspace transitions and media fallbacks remain direct tools.
"""


def code_mode_surface(tools: list) -> list:
    """Filter schemas only; the owning ToolNode retains the complete inventory."""
    from novacode_cli.config.browser_mode import browser_mode_enabled
    from novacode_cli.config.plan_mode import active_plan_session

    if browser_mode_enabled():
        return tools

    # Planning forbids interpreters; retain its existing read-only direct tool path.
    if getattr(active_plan_session.get(), "plan_mode_enabled", False):
        return [
            t
            for t in tools
            if (t.get("name") if isinstance(t, dict) else t.name) not in {"eval", "MCPCode"}
        ]
    return [
        t
        for t in tools
        if (t.get("name") if isinstance(t, dict) else t.name)
        in (DIRECT_ONLY_TOOLS - {"MCPTool", "task"})
        | {"read_file", "search_tools"}
        | SEARCH_VARIANTS
    ]


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
        "start_async_task",
        "check_async_task",
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


class TaskInput(BaseModel):
    """The library's public JavaScript delegation API."""

    model_config = ConfigDict(extra="forbid", strict=True)
    namespace: Literal["task"]
    description: str = Field(min_length=1)
    subagent_type: str = Field(min_length=1, alias="subagentType")
    response_schema: dict[str, Any] | None = Field(default=None, alias="responseSchema")
    label: str | None = None


class CodeModeMiddleware(CodeInterpreterMiddleware):
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
        super().__init__(
            memory_limit=memory_limit,
            timeout=cpu_timeout,
            max_ptc_calls=None,
            max_result_chars=MAX_OUTPUT_CHARS,
            subagents=False,
            mode="thread",
        )
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
        self._state_warnings: dict[str, str] = {}
        self._executions: set[str] = set()

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
                    "task({description, subagentType, responseSchema?}) delegates "
                    "to a configured agent. "
                    "await mcp.list({server_name?, offset?, limit?}); "
                    "await mcp.describe(server_name, tool_name) returns the original schema; "
                    "await mcp.call(server_name, tool_name, arguments) returns structured data. "
                    "Use exact names/schema from discovery. Supports loops and Promise.all. "
                    "No direct filesystem, network or imports. "
                    "State persists in this conversation. "
                    "Nested calls retain permissions and pause for human approval. "
                    "Text file reads are supported; media reads require direct read_file calls. "
                    "Do not retry writes blindly after partial failures or timeouts."
                ),
            )
        ]
        self.tools.insert(
            0,
            self.tools[0].model_copy(
                update={
                    "name": "eval",
                    "description": (
                        "Primary control surface: evaluate JavaScript with top-level await. "
                        "Variables persist across snippets and user turns. Select results with "
                        "the final expression or bounded console.log output. "
                        + self.tools[0].description.replace(
                            "code is an async function body: return the final small result. ", ""
                        )
                    ),
                }
            ),
        )

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Report failed restoration instead of silently claiming variables survived."""
        update = await super().abefore_agent(state, runtime)
        if update is not None:
            thread = _resolve_thread_id(self._fallback_thread_id)
            await self._registry.aevict(thread)
            self._state_warnings[thread] = (
                "JavaScript state restoration failed; variables were cleared."
            )
        return update

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Keep the installed snapshot lifecycle and surface snapshot loss."""
        update = await super().aafter_agent(state, runtime)
        if update is not None and update.get("_quickjs_snapshot_payload") is None:
            thread = _resolve_thread_id(self._fallback_thread_id)
            self._state_warnings[thread] = (
                "JavaScript state could not be saved; variables were cleared."
            )
        return update

    @staticmethod
    def _model_request(request: ModelRequest, *, enabled: bool) -> ModelRequest:
        if enabled:
            from deepagents.middleware._utils import append_to_system_message

            from novacode_cli.config.browser_mode import browser_mode_enabled
            from novacode_cli.config.plan_mode import active_plan_session

            if browser_mode_enabled() or getattr(
                active_plan_session.get(), "plan_mode_enabled", False
            ):
                return request

            return request.override(
                tools=code_mode_surface(request.tools),
                system_message=append_to_system_message(request.system_message, CODE_MODE_GUIDANCE),
            )
        return request.override(
            tools=[
                t
                for t in request.tools
                if (t.get("name") if isinstance(t, dict) else t.name) not in {"MCPCode", "eval"}
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
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error" if error else "success",
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """Fail closed in sync graphs; Nova's normal agent execution is async."""
        if request.tool_call["name"] not in {"MCPCode", "eval"}:
            return handler(request)
        return self._message(request, "MCPCode requires asynchronous agent execution.")

    async def awrap_tool_call(  # noqa: PLR0911 -- explicit fail-closed interpreter gates.
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Route each nested call through the remaining middleware and ToolNode."""
        if request.tool_call["name"] not in {"MCPCode", "eval"}:
            return await handler(request)
        if not await asyncio.to_thread(lambda: NovaConfig().get_code_mode_enabled()):
            return self._message(request, "MCP Code Mode is disabled.")
        if get_policy().tool_default("eval") == "deny":
            return self._message(request, "Code Mode denied by approval policy.")
        if get_policy().tool_default("MCPCode") == "deny":
            return self._message(request, "MCPCode denied by approval policy.")
        if (
            get_policy().tool_default("MCPCode") == "ask"
            or get_policy().tool_default("eval") == "ask"
        ) and not self._auto_approval_enabled():
            return self._message(
                request, "MCPCode requires approval; configure its policy before use."
            )

        async def execute_code(code: str) -> ToolMessage | Command:
            params = CodeInput(code=code)
            thread = _resolve_thread_id(self._fallback_thread_id)
            if thread in self._executions:
                return self._message(request, "Concurrent interpreter execution is unsupported.")
            self._executions.add(thread)
            try:
                return await self._execute(request, handler, params.code)
            except asyncio.CancelledError:
                await self._registry.aevict(thread)
                raise
            finally:
                self._executions.discard(thread)

        # Execute inside ToolNode, so tool callbacks and downstream wrappers see
        # the actual duration, result and failures, not a preliminary probe.
        live_tool = next(t for t in self.tools if t.name == request.tool_call["name"]).model_copy(
            update={"coroutine": execute_code}
        )
        try:
            return await handler(request.override(tool=live_tool))
        except Exception as exc:
            return self._message(request, f"MCPCode failed: {type(exc).__name__}: {exc}"[:4000])

    async def _execute(
        self, request: ToolCallRequest, handler: Callable, code: str
    ) -> ToolMessage | Command:
        from novacode_cli.agents.code_runtime import run_script

        calls = failures = 0
        semaphore = asyncio.Semaphore(self._concurrency)
        updates: dict[str, Any] = {}

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
                    if isinstance(payload, dict) and payload.get("namespace") == "task":
                        value = await self._task(request, handler, payload, updates)
                    elif isinstance(payload, dict) and payload.get("namespace") == "tools":
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
                repl=self._registry.get(_resolve_thread_id(self._fallback_thread_id)),
                runtime=request.runtime,
                expression=request.tool_call["name"] == "eval",
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
            warning = self._state_warnings.pop(_resolve_thread_id(self._fallback_thread_id), None)
            if warning is not None:
                body["state_warning"] = warning
            message = self._message(request, json.dumps(body, ensure_ascii=False), error=False)
            if updates:
                return Command(update={**updates, "messages": [message]})
            return message  # noqa: TRY300 -- same error boundary covers output serialization.
        except Exception as exc:
            message = self._message(
                request,
                json.dumps(
                    {
                        "error": f"{type(exc).__name__}: {exc}"[:4000],
                        "calls": calls,
                        "failed_calls": failures,
                    }
                ),
            )
            return Command(update={**updates, "messages": [message]}) if updates else message

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
        if params.action == "describe" and params.tool_name == "task":
            target = next((t for t in request.runtime.tools if t.name == "task"), None)
        if target is None:
            raise ValueError(
                "Unknown or unsupported tool; use tools.list() or call the tool directly."
            )
        if params.action == "describe":
            return {"name": target.name, "schema": convert_to_openai_tool(target)["function"]}
        return await self._invoke(request, handler, target, params.arguments)

    async def _task(
        self, request: ToolCallRequest, handler: Callable, payload: dict, updates: dict
    ) -> Any:
        """Delegate through the same guards, retaining only supported parent updates."""
        import time

        from langchain_quickjs._subagent import (
            _ensure_schema_title,
            _runtime_with_response_format,
            _validate_response_schema,
        )

        params = TaskInput.model_validate(payload)
        target = next((t for t in request.runtime.tools if t.name == "task"), None)
        if target is None:
            raise ValueError("No subagent task tool configured.")
        if params.response_schema is not None:
            _validate_response_schema(params.response_schema)
            runtime = _runtime_with_response_format(
                request.runtime, _ensure_schema_title(params.response_schema)
            )
            request = replace(request, runtime=runtime)
        writer = request.runtime.stream_writer
        task_id = f"mcp-code-{uuid4().hex}"
        started = time.monotonic()
        event = {"type": "subagent", "id": task_id, "eval_id": request.tool_call["id"]}
        writer(
            {
                **event,
                "phase": "start",
                "description": params.description,
                "subagent_type": params.subagent_type,
                "label": (params.label or params.description)[:80],
            }
        )
        try:
            from novacode_cli.agents.code_runtime import execution_deadline

            deadline = execution_deadline.get()
            if deadline is None:
                raise ValueError("Code Mode execution has ended.")
            # Delegation uses the subagent's own execution limits.
            with deadline.paused():
                value = await self._invoke(
                    request,
                    handler,
                    target,
                    {
                        "description": params.description,
                        "subagent_type": params.subagent_type,
                    },
                    updates=updates,
                    call_id=task_id,
                )
            if params.response_schema is not None and isinstance(value, str):
                value = json.loads(value)
            if params.response_schema is not None:
                _validator(json.dumps(params.response_schema), close_named=False).validate(value)
            writer(
                {
                    **event,
                    "phase": "complete",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return value  # noqa: TRY300 -- completion is part of the traced dispatch.
        except BaseException as exc:
            writer(
                {
                    **event,
                    "phase": "error",
                    "error": str(exc),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            raise

    async def _call(self, request: ToolCallRequest, handler: Callable, params: MCPInput) -> Any:
        target = resolve_target(self._mcp, params)
        if target is None:
            raise ValueError("Unknown or disabled MCP tool; use mcp.list().")
        return await self._invoke(request, handler, target, params.arguments, mcp=True)

    async def _invoke(  # noqa: PLR0912 -- validate and execute at the common trust boundary.
        self,
        request: ToolCallRequest,
        handler: Callable,
        target: BaseTool,
        arguments: dict,
        *,
        mcp: bool = False,
        updates: dict | None = None,
        call_id: str | None = None,
    ) -> Any:
        schema = convert_to_openai_tool(target)["function"]["parameters"]
        try:
            _validator(json.dumps(schema, sort_keys=True), close_named=True).validate(arguments)
        except Exception as exc:
            raise ValueError(f"Invalid tool arguments: {exc}") from exc
        edited = await self._authorize(request, target.name, arguments)
        if edited is not None:
            arguments = edited
            _validator(json.dumps(schema, sort_keys=True), close_named=True).validate(arguments)
        if mcp and get_policy().tool_default("MCPTool") == "deny":
            raise ValueError("MCPTool denied by approval policy.")
        call_id = call_id or f"mcp-code-{uuid4().hex}"
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
        deadline = asyncio.timeout(None if updates is not None else self._call_timeout)
        async with deadline:
            try:
                result = await handler(nested)
            except ValueError as exc:
                if updates is not None and "matching ToolMessage in Command.update" in str(exc):
                    # ToolNode's diagnostic includes the child messages verbatim.
                    raise TypeError("Subagent returned unsupported messages.") from None
                raise
        # Some MCP adapters convert cancellation into a textual tool result.
        if deadline.expired():
            raise TimeoutError("MCP tool call timeout.")
        if isinstance(result, Command) and updates is not None:
            if result.goto or result.graph or not isinstance(result.update, dict):
                raise TypeError("Subagent returned unsupported graph mutation.")
            if set(result.update) - {"messages", "files"}:
                raise TypeError("Subagent returned unsupported state mutation.")
            messages = result.update.get("messages", [])
            if len(messages) != 1 or not isinstance(messages[0], ToolMessage):
                raise TypeError("Subagent returned unsupported messages.")
            files = result.update.get("files", {})
            if files:
                updates.setdefault("files", {}).update(files)
            result = messages[0]
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

    async def _authorize(self, request: ToolCallRequest, name: str, arguments: dict) -> dict | None:
        """Reuse session permissions and optional approval classifiers."""
        from novacode_cli.core.code_approvals import current_code_approvals
        from novacode_cli.security.approval_context import request_user_text
        from novacode_cli.ui.hitl_approval import evaluate_tool_actions

        broker = current_code_approvals.get()
        if broker is not None:
            broker.ensure_active()
        user_request = request_user_text(request)
        decisions = await asyncio.to_thread(
            evaluate_tool_actions,
            {"action_requests": [{"name": name, "args": arguments}]},
            SimpleNamespace(auto_approve=self._auto_approval_enabled()),
            user_request=user_request,
        )
        decision = decisions[0] if len(decisions) == 1 else None
        if decision is None:
            from novacode_cli.agents.code_runtime import execution_deadline

            if broker is None or request.runtime is None:
                raise ValueError(f"{name} requires approval; no active approval handler.")
            deadline = execution_deadline.get()
            if deadline is None:
                raise ValueError("Code Mode execution has ended.")
            with deadline.paused():
                response = await broker.request(
                    request.runtime.stream_writer,
                    {
                        "action_requests": [{"name": name, "args": arguments}],
                        "review_configs": [
                            {
                                "action_name": name,
                                "allowed_decisions": ["approve", "edit", "reject"],
                            }
                        ],
                    },
                )
            choices = response.get("decisions", [])
            decision = choices[0] if len(choices) == 1 else None
            if not isinstance(decision, dict):
                raise ValueError("Missing approval decision.")
            edited = None
            if decision.get("type") == "edit":
                action = decision.get("edited_action", {})
                if action.get("name") != name or not isinstance(action.get("args"), dict):
                    raise ValueError("Approval edits must retain the exact tool name.")
                edited = action["args"]
            # A decision cannot authorize permissions revoked while the script waited.
            refreshed = await asyncio.to_thread(
                evaluate_tool_actions,
                {
                    "action_requests": [
                        {"name": name, "args": edited if edited is not None else arguments}
                    ]
                },
                SimpleNamespace(auto_approve=False),
                user_request=user_request,
            )
            if refreshed and refreshed[0] and refreshed[0].get("type") == "reject":
                raise ValueError(f"{name} denied after permission change.")
            if edited is not None:
                return edited
        if decision.get("type") != "approve":
            raise ValueError(f"{name} denied: {decision.get('reason', 'approval rejected')}")
        return None

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
