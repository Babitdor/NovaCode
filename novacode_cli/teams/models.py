"""Immutable team identities and explicit capability limits."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.messages import AnyMessage

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
GatewayCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

SpawnMode = Literal["default", "fork", "worktree", "remote"]


def operation_digest(name: str, args: dict[str, Any], call_id: str) -> str:
    """Bind a decision to losslessly encoded tool arguments and call identity."""
    encoded = json.dumps([name, args, call_id], sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class TeamError(ValueError):
    """An invalid or unauthorized coordination operation."""


class UncertainStartupError(TeamError):
    """Retain a handle when a failed launch may still own a live worker."""

    def __init__(self, handle: AgentHandle) -> None:
        """Keep cleanup reachable rather than silently losing the worker."""
        super().__init__("Worker startup is uncertain; inspect and cancel its agent record")
        self.handle = handle


@dataclass(frozen=True)
class Identity:
    """Trusted caller identity, bound by the host, never tool arguments."""

    project: str
    root: str
    agent: str = "lead"
    team: str | None = None


@dataclass(frozen=True)
class Limits:
    """Per-team payload limits; the coordinator also bounds live agents per root."""

    agents: int = 8
    pending_messages: int = 32
    messages: int = 256
    message_bytes: int = 16 * 1024
    result_bytes: int = 64 * 1024
    tasks: int = 256
    turns: int = 256
    message_depth: int = 8
    authorizations: int = 8192
    lease_seconds: float = 60.0


@dataclass(frozen=True)
class SpawnRequest:
    """Host-validated worker configuration, never arbitrary model launch args."""

    identity: Identity
    task: str
    mode: str
    agent_type: str
    cwd: Path
    messages: tuple[AnyMessage, ...] = ()
    context_hash: str = ""
    auto_approve: bool = False


class AgentHandle(Protocol):
    """An adapter proves termination before its reservation can be released."""

    async def send(self, text: str) -> None:
        """Wake an idle agent without racing its active invocation."""
        ...

    async def stop(self) -> bool:
        """Stop the worker and return whether termination is certain."""
        ...


class Adapter(Protocol):
    """Execution location; coordination and identity remain host-owned."""

    async def start(
        self,
        request: SpawnRequest,
        event: Callable[[str, dict[str, Any]], Awaitable[None]],
        gateway: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> AgentHandle:
        """Start without issuing model work until the host accepts the worker."""
        ...
