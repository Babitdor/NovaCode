"""Model-facing tools use a host-bound endpoint, never caller-supplied identity."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain.tools import ToolRuntime, tool

from novacode_cli.teams.models import Identity, TeamError

if TYPE_CHECKING:
    from novacode_cli.teams.coordinator import TeamCoordinator

Endpoint = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Binding:
    """Trusted dispatch context; worker pipes construct their own endpoints."""

    identity: Identity
    endpoint: Endpoint
    coordinator: TeamCoordinator | None = None


current_binding: ContextVar[Binding | None] = ContextVar("nova_team_binding", default=None)


async def request(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fail closed outside a team runtime; return bounded tool errors."""
    binding = current_binding.get()
    if binding is None:
        return {"error": "Agent teams are unavailable in this execution context"}
    try:
        return await binding.endpoint(operation, payload)
    except (TeamError, KeyError, TypeError, ValueError) as exc:
        return {"error": str(exc)}


@tool
async def team_create(name: str, swarm: bool = False) -> dict[str, Any]:  # noqa: FBT001, FBT002 - Model tool schema.
    """Create a durable team without starting agents. Swarm requires a separate feature flag."""
    return await request("team_create", {"name": name, "swarm": swarm})


@tool
async def team_delete(team_id: str) -> dict[str, Any]:
    """Stop this team's owned agents and archive its records; never discard worktrees."""
    return await request("team_delete", {"team_id": team_id})


@tool
async def spawn_agent(
    task: str,
    runtime: ToolRuntime,
    mode: str = "default",
    team_id: str | None = None,
    agent_type: str = "general-purpose",
) -> dict[str, Any]:
    """Spawn a persistent teammate: default in-process, fork, worktree, or container remote.

    Default inherits completed conversation context. Other modes receive only the
    explicit task handoff. Inspect with get_agent; do not poll in a model loop.
    """
    binding = current_binding.get()
    if binding is None or binding.coordinator is None:
        return {"error": "Only the root lead may spawn teammates"}
    from novacode_cli.tools.plan_mode_tools import _auto_approve_var

    try:
        return await binding.coordinator.spawn(
            binding.identity,
            task,
            team_id,
            mode=mode,
            agent_type=agent_type,
            messages=runtime.state.get("messages", []),
            auto_approve=_auto_approve_var.get(),
        )
    except (TeamError, ValueError, OSError) as exc:
        return {"error": str(exc)}


@tool
async def list_agents(team_id: str) -> dict[str, Any]:
    """List this team's stable agent IDs, modes, lifecycle and completion summaries."""
    return await request("list_agents", {"team_id": team_id})


@tool
async def get_agent(agent_id: str) -> dict[str, Any]:
    """Inspect one teammate's status and result without reading its private messages."""
    return await request("get_agent", {"agent_id": agent_id})


@tool
async def cancel_agent(agent_id: str) -> dict[str, Any]:
    """Cancel only this agent and owned descendants; preserve unfinished work."""
    return await request("cancel_agent", {"agent_id": agent_id})


@tool
async def send_message(
    team_id: str, to_agent: str, message: str, request_id: str, reply_to: str | None = None
) -> dict[str, Any]:
    """Send bounded peer data using a stable request ID for safe retry deduplication."""
    return await request(
        "send_message",
        {
            "team_id": team_id,
            "to_agent": to_agent,
            "message": message,
            "request_id": request_id,
            "reply_to": reply_to,
        },
    )


@tool
async def read_messages(team_id: str | None = None) -> dict[str, Any]:
    """Read your unread inbox; the lead may omit team_id to read across owned teams."""
    return await request("read_messages", {"team_id": team_id})


@tool
async def ack_message(message_id: str) -> dict[str, Any]:
    """Acknowledge delivery to your inbox, independently of task completion."""
    return await request("ack_message", {"message_id": message_id})


@tool
async def task_create(
    team_id: str, title: str, description: str, depends_on: list[str] | None = None
) -> dict[str, Any]:
    """Lead creates a task with constraints and validation criteria and existing dependencies."""
    return await request(
        "task_create",
        {
            "team_id": team_id,
            "title": title,
            "description": description,
            "depends_on": depends_on,
        },
    )


@tool
async def task_list(team_id: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
    """Page task summaries and versions. Use task_get for full constraints and results."""
    return await request("task_list", {"team_id": team_id, "offset": offset, "limit": limit})


@tool
async def task_get(task_id: str) -> dict[str, Any]:
    """Inspect one shared task and its evidence references."""
    return await request("task_get", {"task_id": task_id})


@tool
async def task_claim(task_id: str, expected_version: int) -> dict[str, Any]:
    """Atomically claim a pending task as yourself. A version conflict means another owner won."""
    return await request("task_claim", {"task_id": task_id, "expected_version": expected_version})


@tool
async def task_update(
    task_id: str, expected_version: int, status: str, result: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Update your owned task with status and verification evidence."""
    return await request(
        "task_update",
        {
            "task_id": task_id,
            "expected_version": expected_version,
            "status": status,
            "result": result,
        },
    )


TEAM_TOOLS = (
    team_create,
    team_delete,
    spawn_agent,
    list_agents,
    get_agent,
    cancel_agent,
    send_message,
    read_messages,
    ack_message,
    task_create,
    task_list,
    task_get,
    task_claim,
    task_update,
)
TEAM_TOOL_NAMES = frozenset(tool.name for tool in TEAM_TOOLS)
