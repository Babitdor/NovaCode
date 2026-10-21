"""Human-in-the-loop interrupt configurations.

This module defines interrupt configurations for tools that require user approval
before execution. Each configuration includes:
- allowed_decisions: What actions the user can take (typically approve/reject)
- description: A function that formats the tool call for the approval prompt

Tool Categories:
- Destructive operations: shell, execute, write_file, edit_file
- External operations: web_search, fetch_url, http_request, browser_automate
- Code execution: run_tests, start_dev_server
- Memory operations: write_memory, create_memory_structure
- User interaction: ask_question

The 11 formatter functions and 11 config blocks are consolidated into a
single generic formatter driven by the ``INTERRUPT_SPECS`` data dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentState
from langchain.messages import ToolCall
from langgraph.runtime import Runtime

# ---------------------------------------------------------------------------
# Schema types
# ---------------------------------------------------------------------------


@dataclass
class FieldSpec:
    """How to format a single tool-call argument in the interrupt prompt.

    Attributes:
        label: Display label (e.g. "Command", "File").
        truncate: If set, truncate string values to this many chars + "...".
        transform: Optional callable to transform the value before display.
        default_display: Fallback string when the arg is missing/None.
    """

    label: str
    truncate: int | None = None
    transform: Callable[[Any], str] | None = None
    default_display: str = "N/A"


@dataclass
class InterruptSpec:
    """Schema-driven spec for a single tool's interrupt configuration.

    Attributes:
        fields: Ordered list of ``(arg_key, FieldSpec)`` tuples.
        static_lines: Lines appended verbatim after the field block.
        warnings: Notice lines prefixed with ⚠️.
    """

    fields: list[tuple[str, FieldSpec]] = field(default_factory=list)
    static_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema data — all 11 tool specs (the only thing that varies)
# ---------------------------------------------------------------------------

INTERRUPT_SPECS: dict[str, InterruptSpec] = {
    "MCPTool": InterruptSpec(fields=[
        ("action", FieldSpec("Action")), ("server_name", FieldSpec("MCP server")),
        ("tool_name", FieldSpec("MCP tool")), ("arguments", FieldSpec("Arguments")),
    ]),
    "CronCreateTool": InterruptSpec(fields=[
        ("cron_expr", FieldSpec("Schedule (local time)")), ("task", FieldSpec("Recurring task")),
    ]),
    "CronDeleteTool": InterruptSpec(fields=[("job_id", FieldSpec("Job ID"))]),
    "EnterWorktreeTool": InterruptSpec(fields=[("name", FieldSpec("Worktree name"))]),
    "ExitWorktreeTool": InterruptSpec(
        static_lines=["Restore original workspace; keep worktree and branch."]
    ),
    "shell": InterruptSpec(
        fields=[
            ("command", FieldSpec("Shell Command")),
        ],
        static_lines=[f"Working Directory: {Path.cwd()}"],
    ),
    "execute": InterruptSpec(
        fields=[
            ("command", FieldSpec("Execute Command")),
        ],
        static_lines=["Location: Remote Sandbox"],
    ),
    "write_file": InterruptSpec(
        fields=[
            ("file_path", FieldSpec("File")),
            ("content", FieldSpec("Content Preview", truncate=200)),
        ],
    ),
    "NotebookEditTool": InterruptSpec(
        fields=[
            ("file_path", FieldSpec("Notebook")),
            ("edit_mode", FieldSpec("Cell action")),
            ("cell_index", FieldSpec("Cell index (zero-based)")),
            ("cell_id", FieldSpec("Cell ID")),
            ("cell_type", FieldSpec("Cell type")),
            ("new_source", FieldSpec("Source preview", truncate=200)),
        ],
    ),
    "edit_file": InterruptSpec(
        fields=[
            ("file_path", FieldSpec("File")),
            ("old_string", FieldSpec("Replace", truncate=100)),
            ("new_string", FieldSpec("With", truncate=100)),
        ],
    ),
    "web_search": InterruptSpec(
        fields=[
            ("query", FieldSpec("Query")),
            ("max_results", FieldSpec("Max results")),
        ],
        warnings=["This will use Tavily API credits"],
    ),
    "fetch_url": InterruptSpec(
        fields=[
            ("url", FieldSpec("URL")),
            ("timeout", FieldSpec("Timeout", transform=lambda v: f"{v}s")),
        ],
        warnings=["Will fetch and convert web content to markdown"],
    ),
    "run_tests": InterruptSpec(
        fields=[
            (
                "command",
                FieldSpec("Test Command", default_display="(auto-detect framework)"),
            ),
            ("working_dir", FieldSpec("Working Directory")),
            ("timeout", FieldSpec("Timeout", transform=lambda v: f"{v}s")),
        ],
        warnings=["Will execute tests and stream output in real-time"],
    ),
    # Gated for the same reason `shell` is: it runs an arbitrary command with
    # shell=True. Without an entry here the tool was never considered by
    # interrupt_on at all, so `daemon(action="start", command=...)` executed
    # unprompted what `shell(command=...)` would have stopped to ask about —
    # and left a process running after the session ended. Absence from this
    # dict fails OPEN, which is the opposite of the policy elsewhere in this
    # module, so a command-executing tool must always be listed.
    "daemon": InterruptSpec(
        fields=[
            ("action", FieldSpec("Action")),
            ("name", FieldSpec("Daemon Name")),
            ("command", FieldSpec("Command", default_display="(none)")),
            ("cwd", FieldSpec("Working Directory", default_display=str(Path.cwd()))),
        ],
        warnings=["Runs detached — it keeps running after Nova exits."],
    ),
    "python_kernel": InterruptSpec(
        fields=[
            ("code", FieldSpec("Python Code", truncate=2000)),
        ],
        static_lines=["Runs in the persistent kernel (namespace survives calls)."],
    ),
    "start_dev_server": InterruptSpec(
        fields=[
            ("command", FieldSpec("Server Command")),
            ("name", FieldSpec("Name")),
            (
                "port",
                FieldSpec("Port", transform=lambda v: str(v) if v else "auto-detect"),
            ),
            ("working_dir", FieldSpec("Working Directory")),
            (
                "auto_open_browser",
                FieldSpec("Auto-open browser", transform=lambda v: "Yes" if v else "No"),
            ),
        ],
        warnings=["Will start a background process (killed on CLI exit)"],
    ),
    "write_memory": InterruptSpec(
        fields=[
            ("memory_type", FieldSpec("Memory Type")),
            ("path", FieldSpec("Path")),
            (
                "append",
                FieldSpec("Mode", transform=lambda v: "Append" if v else "Replace"),
            ),
            ("content", FieldSpec("Content Preview", truncate=100)),
        ],
        warnings=["Will write to memory file"],
    ),
    "duckduckgo_search": InterruptSpec(
        fields=[
            ("query", FieldSpec("Query")),
            ("max_results", FieldSpec("Max results")),
            ("topic", FieldSpec("Topic")),
        ],
        warnings=["Will make web search requests"],
    ),
    "docs_search": InterruptSpec(
        fields=[
            ("query", FieldSpec("Query")),
            ("max_results", FieldSpec("Max results")),
            ("topic", FieldSpec("Topic")),
        ],
        warnings=["Will make web search requests"],
    ),
}

# The single args_schema for each tool — still unique per tool.
# Kept separate from InterruptSpec because schemas are JSON blobs, not formatting rules.
_INTERRUPT_ARG_SCHEMAS: dict[str, dict[str, Any]] = {
    "NotebookEditTool": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "new_source": {"type": ["string", "null"]},
            "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"]},
            "cell_index": {"type": ["integer", "null"], "minimum": 0},
            "cell_id": {"type": ["string", "null"]},
            "cell_type": {"type": ["string", "null"], "enum": ["code", "markdown", "raw", None]},
        },
        "required": ["file_path"],
    },
    "MCPTool": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "describe", "call"]},
            "server_name": {"type": "string"},
            "tool_name": {"type": "string"},
            "arguments": {"type": "object"},
        },
    },
    "CronCreateTool": {
        "type": "object",
        "properties": {"cron_expr": {"type": "string"}, "task": {"type": "string"}},
        "required": ["cron_expr", "task"],
    },
    "CronDeleteTool": {
        "type": "object", "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    "shell": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 120,
            },
        },
        "required": ["command"],
    },
    "execute": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command to execute in sandbox",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 120,
            },
        },
        "required": ["command"],
    },
    "write_file": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to write"},
            "content": {"type": "string", "description": "File content"},
        },
        "required": ["file_path", "content"],
    },
    "edit_file": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to edit"},
            "old_string": {"type": "string", "description": "Text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    "web_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {
                "type": "integer",
                "description": "Max results",
                "default": 5,
            },
        },
        "required": ["query"],
    },
    "fetch_url": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 30,
            },
        },
        "required": ["url"],
    },
    "run_tests": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Test command"},
            "working_dir": {"type": "string", "description": "Working directory"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 300,
            },
        },
        "required": [],
    },
    "start_dev_server": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Server start command"},
            "name": {"type": "string", "description": "Server name"},
            "port": {"type": "integer", "description": "Port number"},
            "working_dir": {"type": "string", "description": "Working directory"},
        },
        "required": ["command"],
    },
    "write_memory": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Memory content to write"},
            "memory_type": {
                "type": "string",
                "description": "Type of memory",
                "enum": ["user", "project"],
            },
            "path": {"type": "string", "description": "Virtual path to write to"},
            "append": {
                "type": "boolean",
                "description": "Append to existing",
                "default": False,
            },
        },
        "required": ["content"],
    },
    "duckduckgo_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {
                "type": "integer",
                "description": "Max results",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Generic formatter
# ---------------------------------------------------------------------------


def _format_interrupt_description(
    tool_call: ToolCall,
    _state: AgentState,
    _runtime: Runtime,
) -> str:
    """Single generic formatter for all tool interrupt descriptions.

    Args:
        tool_call: The tool call being interrupted.
        _state: Unused agent state.
        _runtime: Unused runtime.

    Returns:
        A human-readable description for the approval prompt.
    """
    tool_name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    spec = INTERRUPT_SPECS.get(tool_name)
    if spec is None:
        # Fallback for tools not in our spec
        return f"Tool: {tool_name}\nArgs: {args}"

    parts: list[str] = []

    # Render each field: "Label: value"
    for arg_key, field in spec.fields:
        value = args.get(arg_key)
        # Treat None and empty-string as "not provided" for default_display
        if value is None or (isinstance(value, str) and value == ""):
            value = field.default_display
        elif field.transform:
            value = field.transform(value)
        elif field.truncate is not None and isinstance(value, str) and len(value) > field.truncate:
            value = value[: field.truncate] + "..."
        parts.append(f"{field.label}: {value}")

    # Static lines
    parts.extend(spec.static_lines)

    # Warnings
    for warning in spec.warnings:
        parts.append(f"\n⚠️  {warning}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Interrupt Configuration Factory
# ---------------------------------------------------------------------------


def get_interrupt_configs() -> dict[str, InterruptOnConfig]:
    """Get all human-in-the-loop interrupt configurations.

    Returns:
        Dictionary mapping tool names to their interrupt configurations.

    Interrupt Categories:
        - Destructive operations: shell, execute, write_file, edit_file
        - External operations: web_search, fetch_url, browser_automate
        - Code execution: run_tests, start_dev_server
        - Memory operations: write_memory
        - Search operations: duckduckgo_search, docs_search
        - User interaction: ask_question

    Each config includes ``"edit"`` in ``allowed_decisions`` so the user
    can modify tool arguments before approval (the input form is built from
    the ``args_schema``).
    """
    # Consult the approval policy: tools it unconditionally allows (e.g. read-only
    # searches) don't need to pause the graph at all, so drop them from the gate.
    # Arg-dependent tools (shell/write/edit/fetch/…) stay gated and are decided
    # per-call by the policy inside the agent loop.
    try:
        from novacode_cli.security.policy import get_policy

        policy = get_policy()
    except Exception:  # noqa: BLE001 — fail closed: gate everything if policy load fails
        policy = None

    configs: dict[str, InterruptOnConfig] = {}
    for tool_name in INTERRUPT_SPECS:
        if (
            policy is not None
            and policy.tool_default(tool_name) == "allow"
            and not policy.has_arg_rules(tool_name)
        ):
            continue
        configs[tool_name] = {
            "allowed_decisions": ["approve", "edit", "reject"],
            "description": _format_interrupt_description,  # type: ignore[typeddict-item]
            "args_schema": _INTERRUPT_ARG_SCHEMAS.get(tool_name, {}),
        }
    return configs


__all__ = [
    "get_interrupt_configs",
]
