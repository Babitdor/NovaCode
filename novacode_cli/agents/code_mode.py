"""Programmatic tool composition through the owning graph's tool middleware."""
# ruff: noqa: INP001, ANN401, BLE001, EM101, EM102, TRY003, TRY301
# JSON payloads are heterogeneous; the tool boundary reports arbitrary server failures.

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_quickjs import CodeInterpreterMiddleware
from langchain_quickjs.middleware import _resolve_thread_id
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field
from quickjs_rs import TimeoutError as QuickJSTimeoutError

from novacode_cli.agents.code_runtime import open_repl
from novacode_cli.agents.strict_tools import _validator
from novacode_cli.agents.tool_offload import save_tool_result
from novacode_cli.agents.tool_search_variants import SEARCH_VARIANTS
from novacode_cli.config.nova_config import live_code_mode_enabled
from novacode_cli.core.tool_results import MAX_INTERMEDIATE_CHARS, capture_file_read, data_text
from novacode_cli.mcp.generic import MCPInput, build_mcp_tool, resolve_target
from novacode_cli.security.policy import get_policy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
    from langchain_core.messages.tool import ToolCall

MAX_ARGUMENT_CHARS = 100000
MAX_OUTPUT_CHARS = 12000
MAX_CHECKPOINTS = 8
MAX_CHECKPOINT_BYTES = 4096
logger = logging.getLogger(__name__)


def _failure_kind(exc: Exception) -> str:
    """Give recovery a stable category across Python and adapter error wrappers."""
    detail = str(exc).lower()
    if isinstance(exc, (TimeoutError, QuickJSTimeoutError)) or "timeout" in detail:
        return "timeout"
    if isinstance(exc, FileNotFoundError) or "not found" in detail:
        return "not_found"
    if "denied" in detail or "approval" in detail:
        return "authorization"
    if "budget" in detail or "size limit" in detail:
        return "budget"
    if "validation" in detail or "invalid tool arguments" in detail:
        return "validation"
    return "tool_error"


def _recovery_hint(exc: Exception, outcomes: list[dict], *, omitted: bool) -> str:
    """Tell the next attempt what it can safely resume."""
    if not outcomes and "SyntaxError" in str(exc):
        return (
            "JavaScript syntax error with no host tool calls. For Markdown, use quoted "
            "strings or join an array of lines with '\\n'. Escape template backticks once."
        )
    hints = []
    if _failure_kind(exc) == "timeout" or any("timeout_seconds" in step for step in outcomes):
        hints.append("Use a smaller snippet or run long local commands as background jobs.")
    if any(step.get("task_id") for step in outcomes):
        hints.append("Check recorded task IDs with wait_for_task or tools.call for get_task_logs.")
    if any(step.get("dispatched") for step in outcomes) or omitted:
        hints.append(
            "Completed side effects remain in place. Resume unstarted work. "
            "Skip completed steps and verify the state of any "
            "interrupted operation before repeating it."
        )
    elif outcomes or not hints:
        hints.append("No host tools started. Fix the reported error before retrying.")
    return " ".join(hints)


CODE_MODE_GUIDANCE = """
## Code Mode
Use eval as your primary control surface for ordinary operations. Compose discovery,
native calls, MCP calls, filtering and bounded Promise.all fan-out in JavaScript.
Prefer one eval call per step. Use Promise.all only for independent operations.
Start discovery with await tools.search({query:'focused capability', limit:5}); it
searches callable native and MCP tools and returns exact invocation targets.
Use tools.describe or mcp.describe only when you need arguments or return contracts.
Keep tools.list/describe/call and mcp.list/describe/call for browsing and execution.
tools.list accepts offset and limit, not query.
JavaScript runs in QuickJS: Node.js Buffer, process and require are unavailable.
Use execute with shell:'native' by default (PowerShell on Windows, platform shell
elsewhere), or shell:'bash' for Bash syntax. Always include the shell selector in
new calls. shell/bash names and execute calls without a selector are legacy paths.
Explicit shell selection never retries a command in another dialect automatically.
Tool calls return native data, often plain text; do not assume an execute result has .stdout.
For local builds, test suites and other commands expected to take more than a few seconds,
call execute with background:true and return its job receipt immediately. Use the reported
task_id with the direct wait_for_task tool. Use tools.call('get_task_logs', {task_id})
in a new snippet to read output. Verify the exit status before claiming success. If
launch was interrupted and no ID was returned, use tools.call('list_background_tasks')
before starting another copy. Other backends may run synchronously.
Use task({description, subagentType, responseSchema?}) for configured subagents,
including explicit @agent requests. Call task(...) inside eval, never tools.call('task', ...).
Before choosing an unfamiliar agent, inspect await tools.describe('task') for valid names.
Example JavaScript for eval's code argument:
```javascript
await task({
  subagentType: 'general-purpose',
  description: 'Inspect the workspace for unhandled exceptions. ' +
    'Return file paths, line numbers and explanations; do not edit files.'
});
```
Give each agent a self-contained goal, relevant verified file paths, constraints and
expected output. Child agents cannot access eval's JavaScript variables; include
needed findings in description. Parallelize independent task calls with Promise.all;
await their results before dependent work. task returns the completed result.
For background delegation, call start_async_task directly outside eval. Its task ID
is not a result; obtain required results with the direct wait_for_task tool before
claiming completion or starting dependent work.
eval accepts statements and top-level await; its final expression is the selected result.
Prefer temporary locals inside await (async () => { ...; return smallResult; })().
Variables persist within this conversation. Keep intermediate data in JavaScript;
return a compact synthesis or explicitly print selected output with console.log.
When knowledge_lookup is available, prefer it for code/documentation orientation:
use tools.describe('knowledge_lookup'), then
await tools.call('knowledge_lookup', {query:'what you need to understand'}).
This checks reusable summaries only. On a miss, discover relevant files and supply
up to four workspace-relative paths to reuse or generate an overview. Generation
uses a model call; use it for useful reusable orientation, not as a mandatory step.
The /cache counters track these lookups, not read_file, shell or provider prompt caching.
Read exact source for edits, quotations and verification even after a cache hit.
Batch independent reads/searches in one eval with Promise.all; return only relevant
passages, matches or changed-file summaries. Do not print a whole file just to locate a symbol.
Use an async IIFE for temporary locals: await (async () => { ... })(); this avoids
top-level const/let redeclaration errors. Save reusable data explicitly on globalThis.
After continuation, use the current scratchpad host path from the system context;
never reuse an old temporary host path from a previous process.
Read text with tools.call('read_file', {file_path:'/exact/discovered/path', offset:0, limit:60}).
Nova filesystem text pages return source text, preserving newlines, without display headers
or loop-warning notes. Use that string directly for edit_file.old_string; do not add headers.
If a raw page exceeds display limits, request a smaller offset/limit instead of a full edit.
Use exact workspace virtual paths from discovery; do not reconstruct Windows host paths.
On file-not-found, use glob/ls to discover the correct path before retrying.
Keep large read batches in variables and return small passages; truncated output is incomplete
evidence. Oversized selected results include output_file when successfully saved: read that
virtual path (UTF-8 JSON) in small pages, even after interpreter state loss. If saving failed,
inspect smaller slices of surviving variables; do not replay side effects to recover output.
For Markdown edits, use quoted strings with newline escapes or arrays joined with '\\n';
backticks inside JavaScript template literals must be escaped exactly once. Never replay a
whole script after a runtime failure without checking which side effects already completed.
Every response includes ordered outcomes for dispatched calls. Inspect their tool names,
statuses and concise errors before retrying. Queued calls may have records with status
not_started; sequential calls never reached have no record. Check outcomes_omitted.
For independent checks, use Promise.allSettled and checkpoint each useful result as it
finishes, for example promise.then(value => tools.checkpoint('short-name', value)).
For sequential work, await the checkpoint before starting the next operation. Use unique
names made of letters, digits, dots, underscores or hyphens. Keep selected JSON values
small and free of secrets. A snippet can save up to eight checkpoints, 4 KiB combined.
The result or error receipt includes these under checkpoints. Read that receipt in the
next snippet's context and continue from the saved data. A checkpoint is an observation,
so confirm current state when freshness matters.
Continue with unstarted work and retry only known safe reads. Check actual state before
repeating a dispatched write, command, or browser action. Successful responses may use
`completed` instead: an ordered list of operation identities, all completed successfully.
The full outcomes remain in the session audit trail. Arguments and tool outputs are
not copied into these records; select the data you need in your JavaScript result.
MCPCode is a compatibility async-function body using the same guarded runtime.
Human approvals pause the live script. Never retry completed side effects after failure.
Interaction, planning, workspace transitions and media fallbacks remain direct tools.
Call image_gen directly for image generation so its own approval and deadline apply.
Call write_todos directly (never inside eval or Promise.all), at most once per response and
only when the plan actually changed — do not re-state an unchanged list. Accidental parallel
direct snapshots are merged by task text; the last status for the same text wins. Use one
snapshot when removing tasks.
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
    surface = [
        t
        for t in tools
        if (t.get("name") if isinstance(t, dict) else t.name)
        in (DIRECT_ONLY_TOOLS - {"MCPTool", "task"})
        | {"read_file", "search_tools"}
        | SEARCH_VARIANTS
    ]
    # Keep the host tool intact; only its model-facing media exception is advertised.
    media_description = (
        "Media fallback only: read an image or PDF through the direct multimodal path. "
        "For source code, Markdown and all other text files, use eval with "
        "await tools.call('read_file', {file_path:'/exact/path', offset:0, limit:60})."
    )
    return [
        (
            {**t, "description": media_description}
            if isinstance(t, dict)
            else t.model_copy(update={"description": media_description})
        )
        if (t.get("name") if isinstance(t, dict) else t.name) == "read_file"
        else t
        for t in surface
    ]


# These require their own graph step, user interaction, or interpreter lifecycle.
DIRECT_ONLY_TOOLS = frozenset(
    {
        "MCPCode",
        "MCPTool",
        "eval",
        "task",
        "write_todos",
        "compress",
        "decompress",
        "search_context",
        "acp_status",
        "handoff_save",  # Owns foreground context; keep on the direct graph tool path.
        "schedule_wakeup",  # Session-scoped control stays on its owning graph path.
        "wait_for_task",
        "start_async_task",
        "check_async_task",
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
        "EnterWorktreeTool",
        "ExitWorktreeTool",
        "image_gen",  # Generation uses its own deadline and approval.
        "computer_screenshot",  # Image results need the direct multimodal tool path.
        "computer_action",
    }
)


class CheckpointInput(BaseModel):
    """A small, explicitly selected observation for the current snippet receipt."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    namespace: Literal["checkpoint"]
    name: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_.-]+$")
    value: Any

    def save(self, checkpoints: dict[str, Any]) -> str:
        """Record the value after validating the entire receipt budget."""
        if self.name in checkpoints:
            raise ValueError("Code Mode checkpoint names must be unique within a snippet.")
        if len(checkpoints) >= MAX_CHECKPOINTS:
            raise ValueError("Code Mode checkpoint count budget exceeded.")
        candidate = {**checkpoints, self.name: self.value}
        if len(json.dumps(candidate, allow_nan=False).encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise ValueError("Code Mode checkpoint size limit exceeded.")
        checkpoints[self.name] = self.value
        return self.name


class NativeInput(BaseModel):
    """Discovery and calls over the owning graph's ordinary tool inventory."""

    model_config = ConfigDict(extra="forbid", strict=True)
    namespace: Literal["tools"]
    action: Literal["list", "describe", "call", "search"]
    query: str = Field(default="", max_length=500)
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
        offload_dir: Path | None = None,
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
        self._offload_dir = offload_dir
        self._state_warnings: dict[str, str] = {}
        self._execution_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

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
                    "await tools.search({query, limit?}) finds native and MCP invocation targets; "
                    "await tools.list({offset?, limit?}); await tools.describe(tool_name); "
                    "await tools.call(tool_name, arguments) returns native data; "
                    "describe reports verified or unknown return contracts. "
                    "task({description, subagentType, responseSchema?}) delegates "
                    "to a configured agent. "
                    "await mcp.list({server_name?, offset?, limit?}); "
                    "await mcp.describe(server_name, tool_name) returns the original schema; "
                    "await mcp.call(server_name, tool_name, arguments) returns native data. "
                    "Use exact names/schema from discovery. Parallelize independent calls only. "
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

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        """Report failed restoration instead of silently claiming variables survived."""
        payload = state.get("_quickjs_snapshot_payload")
        if not payload:
            return None
        thread = _resolve_thread_id(self._fallback_thread_id)
        async with self._execution_locks.setdefault(thread, asyncio.Lock()):
            try:
                repl = await open_repl(self._registry, thread)
                await repl.arestore_snapshot(payload, inject_globals=True)
            except asyncio.CancelledError:
                await self._registry.aevict(thread)
                raise
            except Exception:
                logger.warning(
                    "Failed to restore QuickJS snapshot for thread_id=%s", thread, exc_info=True
                )
                await self._registry.aevict(thread)
                self._state_warnings[thread] = (
                    "JavaScript state restoration failed; variables were cleared."
                )
                return {"_quickjs_snapshot_payload": None}
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Keep the installed snapshot lifecycle and surface snapshot loss."""
        thread = _resolve_thread_id(self._fallback_thread_id)
        async with self._execution_locks.setdefault(thread, asyncio.Lock()):
            update = await super().aafter_agent(state, runtime)
        if update is not None and update.get("_quickjs_snapshot_payload") is None:
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
        return handler(self._model_request(request, enabled=live_code_mode_enabled()))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> Any:
        """Apply preference changes to existing graphs without rebuilding them."""
        enabled = await asyncio.to_thread(live_code_mode_enabled)
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
        if not await asyncio.to_thread(live_code_mode_enabled):
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
            lock = self._execution_locks.setdefault(thread, asyncio.Lock())
            # One persistent VM per conversation; queue separate scripts, not their host calls.
            async with lock:
                return await self._execute(request, handler, params.code)

        # Execute inside ToolNode, so tool callbacks and downstream wrappers see
        # the actual duration, result and failures, not a preliminary probe.
        live_tool = next(t for t in self.tools if t.name == request.tool_call["name"]).model_copy(
            update={"coroutine": execute_code}
        )
        try:
            return await handler(request.override(tool=live_tool))
        except Exception as exc:
            return self._message(request, f"MCPCode failed: {type(exc).__name__}: {exc}"[:4000])

    async def _execute(  # noqa: PLR0915 -- guarded dispatch and bounded result reporting.
        self, request: ToolCallRequest, handler: Callable, code: str
    ) -> ToolMessage | Command:
        from novacode_cli.agents.code_runtime import run_script

        calls = failures = 0
        outcomes: list[dict[str, Any]] = []
        checkpoints: dict[str, Any] = {}
        semaphore = asyncio.Semaphore(self._concurrency)
        updates: dict[str, Any] = {}

        async def dispatch(raw: str) -> str:  # noqa: PLR0912 -- Dispatch and record guarded calls.
            nonlocal calls, failures
            calls += 1
            step: dict[str, Any] = {"step": calls, "status": "not_started"}
            if len(outcomes) <= self._max_calls:
                outcomes.append(step)
            try:
                if calls > self._max_calls:
                    raise ValueError("MCPCode call budget exceeded.")
                if not isinstance(raw, str) or len(raw) > MAX_ARGUMENT_CHARS:
                    raise ValueError("MCPCode arguments exceed the size limit.")
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    step.update(
                        {
                            key: payload[key][:160]
                            for key in ("namespace", "action", "server_name", "tool_name")
                            if isinstance(payload.get(key), str)
                        }
                    )
                async with semaphore:
                    step["status"] = "running"
                    namespace = payload.get("namespace") if isinstance(payload, dict) else None
                    if namespace == "checkpoint":
                        checkpoint = CheckpointInput.model_validate(payload)
                        value = checkpoint.save(checkpoints)
                    elif namespace == "task":
                        value = await self._task(request, handler, payload, updates, step)
                    elif namespace == "tools":
                        native = NativeInput.model_validate(payload)
                        value = await self._native(request, handler, native, step)
                    else:
                        params = MCPInput.model_validate(payload, strict=True)
                        if get_policy().tool_default("MCPTool") == "deny":
                            raise ValueError("MCPTool denied by approval policy.")
                        if self._catalog is None:
                            raise ValueError("No MCP tools configured. Use tools.list().")
                        if params.action != "call":
                            value = self._catalog.invoke(params.model_dump())
                        else:
                            value = await self._call(request, handler, params, step)
                    # The operation completed even if serialization subsequently fails.
                    step["status"] = "completed"
                    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                    if len(encoded) > MAX_INTERMEDIATE_CHARS:
                        raise ValueError(
                            "Tool result exceeds 1000000 characters; request fewer records."
                        )
                    return encoded
            except asyncio.CancelledError:
                if step["status"] == "running":
                    step["status"] = "cancelled"
                raise
            except Exception as exc:
                failures += 1
                if step["status"] != "completed":
                    step["status"] = "failed"
                step["error"] = {
                    "kind": _failure_kind(exc),
                    "type": type(exc).__name__,
                    "message": str(exc).strip().splitlines()[-1][:300] if str(exc).strip() else "",
                }
                raise

        try:
            thread = _resolve_thread_id(self._fallback_thread_id)
            result, error = await run_script(
                code,
                dispatch,
                cpu_timeout=self._cpu_timeout,
                wall_timeout=self._wall_timeout,
                memory_limit=self._memory_limit,
                registry=self._registry,
                thread=thread,
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
                "outcomes": outcomes,
                "outcomes_omitted": calls - len(outcomes),
                "recovery": (
                    "Do not replay completed calls. Failed/cancelled calls may have side effects."
                ),
            }
            if checkpoints:
                body["checkpoints"] = checkpoints
            if truncated:
                saved = await asyncio.to_thread(
                    save_tool_result,
                    self._offload_dir,
                    request.tool_call["id"] or "selected-result",
                    request.tool_call["name"],
                    json.dumps(result, ensure_ascii=True, allow_nan=False),
                )
                if saved is not None:
                    body["output_file"] = {"path": saved, "encoding": "utf-8", "format": "json"}
                body["notice"] = (
                    "Preview is incomplete. Read output_file.path for the complete selected "
                    "result, encoded as UTF-8 JSON. Do not replay completed side effects."
                    if saved is not None
                    else "Preview is incomplete. Saving failed or offload storage "
                    "is unavailable; no recoverable file was saved. Inspect smaller slices of "
                    "surviving interpreter variables. Do not replay completed side effects."
                )
            warning = self._state_warnings.pop(_resolve_thread_id(self._fallback_thread_id), None)
            if warning is not None:
                body["state_warning"] = warning
            message = self._message(request, json.dumps(body, ensure_ascii=False), error=False)
            if updates:
                return Command(update={**updates, "messages": [message]})
            return message  # noqa: TRY300 -- same error boundary covers output serialization.
        except (Exception, asyncio.CancelledError) as exc:
            failure = (
                ValueError("Code Mode interrupted.")
                if isinstance(exc, asyncio.CancelledError)
                else exc
            )
            hint = _recovery_hint(failure, outcomes, omitted=calls > len(outcomes))
            body = {
                "error": (
                    str(failure)
                    if isinstance(exc, asyncio.CancelledError)
                    else f"{type(failure).__name__}: {failure}"[:4000]
                ),
                "calls": calls,
                "failed_calls": failures,
                "outcomes": outcomes,
                "outcomes_omitted": calls - len(outcomes),
                "hint": hint,
            }
            if checkpoints:
                body["checkpoints"] = checkpoints
            warning = self._state_warnings.pop(_resolve_thread_id(self._fallback_thread_id), None)
            if warning is not None:
                body["state_warning"] = warning
            message = self._message(
                request,
                json.dumps(body),
            )
            if isinstance(exc, asyncio.CancelledError):
                from novacode_cli.core.code_approvals import current_code_approvals

                broker = current_code_approvals.get()
                if broker is not None and broker.valid() and (call_id := message.tool_call_id):
                    broker.interrupted[call_id] = (request.tool_call, message)
                raise
            return Command(update={**updates, "messages": [message]}) if updates else message

    async def _native(
        self, request: ToolCallRequest, handler: Callable, params: NativeInput, step: dict
    ) -> Any:
        available = _native_tools(request)
        from novacode_cli.shell.tool_surface import execution_tools

        public = {t.name: t for t in execution_tools(list(available.values()))}
        if params.action == "search":
            return await self._search(request, public, params.query, params.limit)
        if params.action == "list":
            entries = [
                {"name": t.name, "description": t.description[:300]} for t in public.values()
            ]
            return {
                "tools": entries[params.offset : params.offset + params.limit],
                "total": len(entries),
                "offset": params.offset,
            }
        target = available.get(params.tool_name)
        if params.action == "describe" or "shell" in params.arguments:
            target = public.get(params.tool_name, target)
        if params.action == "describe" and params.tool_name == "task":
            target = next((t for t in request.runtime.tools if t.name == "task"), None)
        if target is None:
            raise ValueError(
                "Unknown or unsupported tool; use tools.list() or call the tool directly."
            )
        if params.action == "describe":
            from novacode_cli.agents.tool_contracts import native_returns

            return {
                "name": target.name,
                "schema": convert_to_openai_tool(target)["function"],
                "returns": native_returns(target),
            }
        return await self._invoke(request, handler, target, params.arguments, step)

    async def _search(
        self, request: ToolCallRequest, available: dict, query: str, limit: int
    ) -> dict:
        """Rank only callable capabilities in this invocation, keeping routing identities."""
        from novacode_cli.agents.tool_search import ToolSearchMiddleware
        from novacode_cli.agents.tool_search_variants import parameter_text
        from novacode_cli.config.browser_mode import browser_mode_enabled

        targets = [
            (target, {"namespace": "tools", "tool_name": name})
            for name, target in available.items()
        ]
        if self._mcp is not None and get_policy().tool_default("MCPTool") != "deny":
            await asyncio.shield(self._mcp.start_background_discovery())
            runtime_ids = {id(tool) for tool in request.runtime.tools}
            by_name: dict[tuple[str | None, str], BaseTool] = {}
            for tool in self._mcp.tools:
                server = (tool.metadata or {}).get("nova_mcp_server")
                by_name.setdefault((server, tool.name), tool)
            seen_routes = set()
            for entry in self._mcp._tools_cache:
                route = (entry["server"], entry["skill_alias"])
                if route in seen_routes:
                    continue
                seen_routes.add(route)
                target = by_name.get((entry["server"], entry["name"]))
                if target is not None and id(target) in runtime_ids:
                    targets.append(
                        (
                            target,
                            {
                                "namespace": "mcp",
                                "server_name": entry["server"],
                                "tool_name": entry["skill_alias"],
                            },
                        )
                    )
        visible = {
            id(target)
            for target in ToolSearchMiddleware._discovery_tools(
                (target for target, _route in targets),
                browsing=browser_mode_enabled(),
            )
        }
        catalog = [
            {
                "name": target.name,
                "description": target.description,
                "kind": route["namespace"] + ":" + route.get("server_name", ""),
                "parameters": parameter_text(target),
                "target": route,
            }
            for target, route in targets
            if id(target) in visible
        ]
        query = query.strip()
        matches = ToolSearchMiddleware._search_matches(query, catalog) if query else []
        return {
            "tools": [
                {
                    "name": hit["name"],
                    "description": hit["description"][:300],
                    "target": hit["target"],
                }
                for hit in matches[:limit]
            ],
            "total": len(matches),
        }

    async def _task(
        self, request: ToolCallRequest, handler: Callable, payload: dict, updates: dict, step: dict
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
        roster = (target.metadata or {}).get("nova_subagent_names")
        if roster is not None and params.subagent_type not in roster:
            raise ValueError(
                f"Unknown subagent {params.subagent_type!r}; use tools.describe('task')."
            )
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
                    step,
                    updates=updates,
                    call_id=task_id,
                )
            if (
                roster is None
                and isinstance(value, str)
                and value.startswith(
                    f"We cannot invoke subagent {params.subagent_type} because it does not exist,"
                )
            ):
                raise ValueError(value)
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

    async def _call(
        self, request: ToolCallRequest, handler: Callable, params: MCPInput, step: dict
    ) -> Any:
        if self._mcp is not None:
            await asyncio.shield(self._mcp.start_background_discovery())
        target = resolve_target(self._mcp, params)
        if target is None:
            raise ValueError("Unknown or disabled MCP tool; use mcp.list().")
        return await self._invoke(request, handler, target, params.arguments, step, mcp=True)

    async def _invoke(  # noqa: PLR0912 -- validate and execute at the common trust boundary.
        self,
        request: ToolCallRequest,
        handler: Callable,
        target: BaseTool,
        arguments: dict,
        step: dict,
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
        nested = replace(
            request,
            tool=target,
            tool_call=call,
            runtime=replace(request.runtime, tool_call_id=call_id),
        )
        with capture_file_read(enabled=target.name == "read_file" and not mcp) as capture:
            result = await self._invoke_with_deadline(handler, nested, target.name, step, updates)
        if (
            not mcp
            and isinstance(result, ToolMessage)
            and isinstance(task_id := result.additional_kwargs.get("nova_shell_task_id"), str)
        ):
            step["task_id"] = task_id
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
        if target.name in {"read_file", "write_file", "edit_file", "ls", "glob", "grep"} and (
            isinstance(value, str) and value.startswith("Error:")
        ):
            hint = (
                " Discover the exact virtual path with glob/ls before retrying; "
                "do not guess a host path."
                if "not found" in value.lower()
                else ""
            )
            raise ValueError(value + hint)
        if isinstance(value, str) and value.startswith("[MCP error]"):
            raise ValueError(value)
        if capture is not None and (raw := capture.source_text(value)) is not None:
            value = raw
        return value

    async def _invoke_with_deadline(
        self,
        handler: Callable,
        nested: ToolCallRequest,
        name: str,
        step: dict,
        updates: dict | None,
    ) -> Any:
        from novacode_cli.core.code_approvals import current_code_approvals

        broker = current_code_approvals.get()
        if broker is not None:
            broker.ensure_active()
        deadline = asyncio.timeout(None if updates is not None else self._call_timeout)
        step["dispatched"] = True
        step.setdefault("tool_name", name)
        failure = None
        result = None
        try:
            async with deadline:
                result = await handler(nested)
        except Exception as exc:
            if not deadline.expired():
                if (
                    updates is not None
                    and isinstance(exc, ValueError)
                    and ("matching ToolMessage in Command.update" in str(exc))
                ):
                    # ToolNode's diagnostic includes the child messages verbatim.
                    raise TypeError("Subagent returned unsupported messages.") from None
                raise
            failure = exc
        if deadline.expired():
            step["timeout_seconds"] = self._call_timeout
            if failure is not None and not isinstance(failure, TimeoutError):
                step["timeout_detail"] = f"{type(failure).__name__}: {failure}"[:300]
            raise TimeoutError(
                f"{name} exceeded its {self._call_timeout:g}s Code Mode tool timeout."
            ) from failure
        return result

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
        value = (
            data_text(result.content, result.additional_kwargs)
            if isinstance(result.content, str)
            else result.content
        )
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
