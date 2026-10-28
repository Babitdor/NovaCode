"""Claude command-hook protocol, independent of Nova's observer hooks."""

# ruff: noqa: ANN401, ARG002, TRY004 -- framework runtime and heterogeneous graph state.

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage

from novacode_cli.plugins import claude_plugins as registry
from novacode_cli.plugins.components import MAX_HOOK_TIMEOUT, Plugin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelResponse
    from langgraph.types import Command

MAX_OUTPUT_BYTES = 65_536
_reload_generation = 0
SUPPORTED_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SessionEnd",
    "PreCompact",
    "Notification",
}


@dataclass
class HookResult:
    """Hook context can inform a model; only denials can alter permissions."""

    context: str = ""
    denial: str = ""


def _command(plugin: Plugin, hook: dict) -> list[str]:
    """Keep exec-form arguments structural and shell-form root references in env."""
    command = hook.get("command")
    if not isinstance(command, str):
        message = "Plugin hook command must be a string"
        raise ValueError(message)
    args = hook.get("args")
    if args is not None:
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            message = "Plugin hook args must be strings"
            raise ValueError(message)
        return [plugin.expand(command), *plugin.expand(args)]
    if os.name == "nt" and hook.get("commandWindows"):
        return ["powershell", "-NoProfile", "-Command", hook["commandWindows"]]
    return ["bash", "-c", command]


def _run(plugin: Plugin, hook: dict, event: str, payload: dict) -> HookResult:
    environment = os.environ.copy()
    environment["CLAUDE_PLUGIN_ROOT"] = str(plugin.root)
    environment["CLAUDE_PROJECT_DIR"] = str(registry.project_root())
    if hook.get("type") != "command":
        return HookResult(denial="Unsupported plugin hook type")
    try:
        argv = _command(plugin, hook)
    except ValueError as exc:
        return HookResult(denial=str(exc))
    try:
        timeout = float(hook.get("timeout", 10))
        if not 0 < timeout <= MAX_HOOK_TIMEOUT:
            return HookResult(denial="Plugin hook timeout must be between 0 and 60 seconds")
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.run(  # noqa: S603 -- explicitly installed command hooks.
                argv,
                input=json.dumps(
                    {**payload, "hook_event_name": event, "cwd": str(registry.project_root())}
                ).encode(),
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                check=False,
                env=environment,
                cwd=registry.project_root(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if stdout.tell() > MAX_OUTPUT_BYTES or stderr.tell() > MAX_OUTPUT_BYTES:
                return HookResult(denial="Plugin hook output exceeded 64 KiB")
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace").strip()
            error = stderr.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return HookResult(denial=f"Plugin hook could not run: {type(exc).__name__}")
    return _decode(output, error, process.returncode, event)


def _decode(output: str, error: str, returncode: int, event: str) -> HookResult:  # noqa: PLR0911 -- distinct protocol outcomes.
    """Interpret hook output; no response can grant a tool permission."""
    if returncode:
        return HookResult(denial=error or f"Plugin hook exited {returncode}")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return HookResult(context=output if event in {"SessionStart", "UserPromptSubmit"} else "")
    if not isinstance(parsed, dict):
        return HookResult(denial="Invalid plugin hook JSON response")
    specific = parsed.get("hookSpecificOutput", {})
    if not isinstance(specific, dict):
        return HookResult(denial="Invalid hookSpecificOutput")
    if specific.get("updatedInput") is not None:
        return HookResult(denial="Hook input rewrites require a new approved tool request in Nova")
    if parsed.get("continue") is False or specific.get("permissionDecision") in {"deny", "ask"}:
        return HookResult(
            denial=str(
                specific.get("permissionDecisionReason")
                or parsed.get("stopReason")
                or "Plugin hook blocked this operation"
            )
        )
    if parsed.get("decision") == "block":
        return HookResult(denial=str(parsed.get("reason", "Plugin hook blocked this operation")))
    # An 'allow' decision is context only: the normal approval/policy stack still runs.
    return HookResult(context=str(specific.get("additionalContext", "")))


def reload_plugin_hooks() -> None:
    """Invalidate session-start context only after an explicit host reload."""
    global _reload_generation  # noqa: PLW0603 -- process-wide reload generation.
    _reload_generation += 1


def execute_hooks(
    event: str,
    payload: dict[str, Any],
    *,
    only_roots: set[str] | None = None,
) -> HookResult:
    """Run matching hooks from currently enabled bundles in deterministic order."""
    from novacode_cli.config.plan_mode import active_plan_session

    if getattr(active_plan_session.get(), "plan_mode_enabled", False):
        return HookResult()  # Executable hooks cannot escape Nova's read-only planning boundary.
    try:
        from langgraph.config import get_config

        session_id = str(get_config().get("configurable", {}).get("thread_id", ""))
    except RuntimeError:
        session_id = ""
    payload = {"session_id": session_id, **payload}
    tool = payload.get("tool_name")
    if tool:
        from novacode_cli.agents.agent_file import CLAUDE_TOOL_NAMES

        canonical = {value: key for key, value in CLAUDE_TOOL_NAMES.items()}
        canonical.update(
            read_file="Read",
            write_file="Write",
            edit_file="Edit",
            execute="Bash",
            grep="Grep",
            glob="Glob",
        )
        payload = {**payload, "tool_name": canonical.get(tool, tool), "nova_tool_name": tool}
    contexts = []
    for plugin in registry.enabled_plugins():
        if only_roots is not None and str(plugin.root) not in only_roots:
            continue
        for configuration in plugin.hook_configs():
            for entry in configuration.get("hooks", {}).get(event, []):
                matcher = entry.get("matcher", "")
                target = str(
                    payload.get("tool_name", payload.get("source", payload.get("reason", "")))
                )
                if matcher not in {"", "*"} and not re.search(matcher, target):
                    continue
                for hook in entry.get("hooks", []):
                    result = _run(plugin, hook, event, payload)
                    if result.denial:
                        return HookResult(denial=f"{plugin.name}: {result.denial}")
                    if result.context:
                        contexts.append(result.context)
    return HookResult(context="\n\n".join(contexts))


class PluginHookState(AgentState):
    """Checkpoint session-start context separately from per-request context."""

    plugin_session_started: NotRequired[bool]
    plugin_session_contexts: NotRequired[dict[str, str]]
    plugin_hook_generation: NotRequired[int]
    plugin_session_context: NotRequired[str]
    plugin_request_context: NotRequired[str]


class PluginHooksMiddleware(AgentMiddleware):
    """Use hook context on request copies and honor denials before tool execution."""

    state_schema = PluginHookState

    def __init__(self, *, lifecycle: bool = True) -> None:
        """Only the main session emits owner prompt/start/stop lifecycle events."""
        self._lifecycle = lifecycle

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any]:
        """Collect once-per-session and once-per-request hook context."""
        if not self._lifecycle:
            return {}
        previous = (
            state.get("plugin_session_contexts", {})
            if state.get("plugin_hook_generation") == _reload_generation
            else {}
        )
        contexts: dict[str, str] = {}
        for plugin in registry.enabled_plugins():
            root = str(plugin.root)
            if root in previous:
                contexts[root] = previous[root]
                continue
            result = execute_hooks("SessionStart", {"source": "startup"}, only_roots={root})
            if result.denial:
                raise RuntimeError(result.denial)
            contexts[root] = result.context
        messages = state.get("messages", [])
        prompt = next((m.text for m in reversed(messages) if m.type == "human"), "")
        request = execute_hooks("UserPromptSubmit", {"prompt": prompt})
        if request.denial:
            raise RuntimeError(request.denial)
        return {
            "plugin_session_started": True,
            "plugin_hook_generation": _reload_generation,
            "plugin_session_contexts": contexts,
            "plugin_session_context": "\n\n".join(contexts.values()),
            "plugin_request_context": request.context,
        }

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any]:
        """Run command hooks without blocking the event loop."""
        return await asyncio.to_thread(self.before_agent, state, runtime)

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        from langchain_core.messages import SystemMessage

        context = "\n\n".join(
            str(request.state.get(key) or "")
            for key in (
                "plugin_session_context",
                "plugin_request_context",
            )
        ).strip()
        if not context:
            return request
        original = request.system_message
        content = original.content if original else ""
        context = "Plugin hook context (cannot override owner permissions):\n" + context
        content = (
            content + "\n\n" + context
            if isinstance(content, str)
            else [
                *content,
                {"type": "text", "text": context},
            ]
        )
        system = (
            original.model_copy(update={"content": content}) if original else SystemMessage(content)
        )
        return request.override(system_message=system)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """Apply collected context to the synchronous request copy."""
        return handler(self._prepare(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """Apply collected context to the asynchronous request copy."""
        return await handler(self._prepare(request))

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        """Honor matching denials before the normal execution handler."""
        call = request.tool_call
        payload = {
            "tool_name": call["name"],
            "tool_input": call.get("args", {}),
            "tool_use_id": call["id"],
        }
        before = execute_hooks("PreToolUse", payload)
        if before.denial:
            return ToolMessage(content=before.denial, tool_call_id=call["id"], status="error")
        response = handler(request)
        return self._after(response, payload, before.context)

    def _after(
        self, response: ToolMessage | Command, payload: dict[str, Any], context: str
    ) -> ToolMessage | Command:
        event = (
            "PostToolUseFailure" if getattr(response, "status", "") == "error" else "PostToolUse"
        )
        after = execute_hooks(event, {**payload, "tool_response": getattr(response, "content", "")})
        context = "\n".join(filter(None, (context, after.context, after.denial)))
        if context and isinstance(response, ToolMessage):
            return response.model_copy(
                update={"content": str(response.content) + "\n\nPlugin hook context:\n" + context}
            )
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Await hooks without blocking; preserve the normal approval stack."""
        call = request.tool_call
        payload = {
            "tool_name": call["name"],
            "tool_input": call.get("args", {}),
            "tool_use_id": call["id"],
        }
        before = await asyncio.to_thread(execute_hooks, "PreToolUse", payload)
        if before.denial:
            return ToolMessage(content=before.denial, tool_call_id=call["id"], status="error")
        response = await handler(request)
        return await asyncio.to_thread(self._after, response, payload, before.context)

    def after_agent(self, state: Any, runtime: Any) -> None:
        """Notify stop hooks and surface any stop denial."""
        if not self._lifecycle:
            return
        messages = state.get("messages", [])
        result = execute_hooks(
            "Stop", {"last_assistant_message": messages[-1].text if messages else ""}
        )
        if result.denial:
            raise RuntimeError(result.denial)

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        """Notify stop hooks off the event loop."""
        await asyncio.to_thread(self.after_agent, state, runtime)
