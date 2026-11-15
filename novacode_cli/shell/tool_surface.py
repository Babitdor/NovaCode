"""One advertised execution surface; registered legacy tools remain callable."""

from __future__ import annotations

from typing import Literal

from langchain.tools import ToolRuntime  # noqa: TC002 -- Pydantic resolves the injected field.
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

ALIAS_MARKER = "nova_execution_alias"


class ExecuteInput(BaseModel):
    """Explicit new calls can be distinguished from saved platform-default calls."""

    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    runtime: ToolRuntime
    command: str = Field(min_length=1)
    shell: Literal["native", "bash"] = Field(
        description="Choose native by default; Bash only when needed."
    )
    timeout: int | None = Field(default=None, ge=0, le=3600)
    interactive: bool = False
    background: bool = False


def is_backend_execute(tool: object) -> bool:
    """Only adapt the backend executor whose downstream owner is Nova's shell."""
    return (
        isinstance(tool, BaseTool)
        and tool.name == "execute"
        and any(
            getattr(fn, "__module__", "") == "deepagents.middleware.filesystem"
            for fn in (getattr(tool, "func", None), getattr(tool, "coroutine", None))
        )
    )


def execution_tool(tool: BaseTool) -> BaseTool:
    """Advertise the schema accepted by ShellMiddleware, without mutating the registry."""
    adapted = tool.model_copy()
    adapted.args_schema = ExecuteInput
    adapted.description = (
        "Execute a command in the configured workspace/backend. Set shell='native' by "
        "default (PowerShell on Windows, platform shell elsewhere); choose 'bash' for "
        "Bash syntax. Sandboxes stay on their backend. Supports interactive/background "
        "execution. timeout may shorten the configured local deadline; backend limits "
        "also apply. An explicit shell never automatically retries in another dialect. "
        "Returns output/status text, sometimes decoded JSON; no stdout property."
    )
    return adapted


def execution_tools(tools: list) -> list:
    """Hide only Nova's marked aliases when their canonical backend tool is present."""
    if not any(is_backend_execute(t) for t in tools) or not any(
        (getattr(t, "metadata", None) or {}).get(ALIAS_MARKER) for t in tools
    ):
        return tools
    return [
        execution_tool(t) if is_backend_execute(t) else t
        for t in tools
        if not (getattr(t, "metadata", None) or {}).get(ALIAS_MARKER)
    ]
