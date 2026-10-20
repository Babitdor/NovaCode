"""Process and strict worktree adapters over Nova's session supervisor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.sessions import protocol as session_protocol
from novacode_cli.sessions.supervisor import SessionSupervisor
from novacode_cli.sessions.worktree import WorktreeInfo
from novacode_cli.teams.models import SpawnRequest, TeamError, UncertainStartupError
from novacode_cli.teams.protocol import VERSION, encode_control, validate_handshake
from novacode_cli.teams.scheduler import PROCESS_BUDGET

if TYPE_CHECKING:
    from collections.abc import Callable

    from novacode_cli.sessions.supervisor import ChildSession


MAX_RPC_REQUESTS = 16


@dataclass
class TeamWorktreeInfo(WorktreeInfo):
    """Keep the scoped workspace without mutating the existing tab contract."""

    workspace: Path
    source: Path


def strict_worktree(workspace: Path, agent_id: str) -> TeamWorktreeInfo:
    """Allocate actual isolation, never fall back to a shared working directory."""
    from novacode_cli.sessions.worktree import (
        create_worktree,
        has_commits,
        remove_worktree,
        repo_root,
    )

    repo = repo_root(workspace)
    if repo is None or not has_commits(repo):
        msg = "Requested worktree isolation requires a Git repository with a commit"
        raise TeamError(msg)
    info = create_worktree(f"team-{agent_id}", repo=repo, session_id=agent_id)
    if info.path.resolve() == workspace.resolve() or not info.created:
        msg = "Requested worktree isolation could not be established"
        raise TeamError(msg)
    scoped = info.path / workspace.resolve().relative_to(repo.resolve())
    if not scoped.is_dir():
        remove_worktree(info.path, repo=repo)
        msg = "The scoped workspace does not exist in the committed worktree"
        raise TeamError(msg)
    return TeamWorktreeInfo(info.path, info.branch, info.created, info.warnings, scoped, workspace)


class ProcessAdapter:
    """Same worker loop over real pipes; worktrees are strict, not best effort."""

    def __init__(
        self, model: dict[str, Any], *, mode: str = "fork", argv: list[str] | None = None
    ) -> None:
        """Only host configuration can supply a model descriptor or launch argv."""
        self.model, self.mode, self.argv = model, mode, argv

    async def start(
        self, request: SpawnRequest, event: Callable, gateway: Callable
    ) -> ProcessHandle:
        """Negotiate identity and capabilities before submitting any model task."""
        info = None
        handle = None
        try:
            if self.mode == "worktree":
                preparation = asyncio.create_task(
                    asyncio.to_thread(strict_worktree, request.cwd, request.identity.agent)
                )
                try:
                    info = await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    info = await preparation
                    raise
                request = replace(request, cwd=info.workspace)
            model = self.model.get("by_agent", {}).get(request.agent_type, self.model)
            if not model.get("provider") or not model.get("model"):
                msg = "The configured model lacks a serializable provider; use default mode"
                raise TeamError(msg)  # noqa: TRY301 - Rollback must include validation failures.
            handle = ProcessHandle(request, model, event, gateway, info)
            await handle.start(self.argv)
            return handle  # noqa: TRY300 - Keep launch and cleanup in one guard.
        except BaseException:
            if handle is not None:
                if not await handle.stop():
                    raise UncertainStartupError(handle) from None
            elif info is not None:
                await _preserve_or_remove(info.path, info.source)
            raise


async def _preserve_or_remove(path: Path, source: Path) -> str:
    from novacode_cli.sessions.worktree import remove_worktree, repo_root

    repo = await asyncio.to_thread(repo_root, source)
    if repo is None:
        return f"Preserved worktree {path}: repository unavailable"
    return await asyncio.to_thread(remove_worktree, path, repo=repo)


class ProcessHandle:
    """One identity-bound pipe, readiness deadline, and serialized message pump."""

    def __init__(
        self,
        request: SpawnRequest,
        model: dict[str, Any],
        event: Callable,
        gateway: Callable,
        worktree: TeamWorktreeInfo | None = None,
    ) -> None:
        """Reserve no external resource until start; retain the exact expected handshake."""
        self.request, self.event, self.gateway = request, event, gateway
        self.worktree = worktree
        self.supervisor = SessionSupervisor(self._message, process_budget=PROCESS_BUDGET)
        self.ready = asyncio.get_running_loop().create_future()
        self.child: ChildSession | None = None
        self.stopped = False
        self.turns = 0
        self._rpc_tasks: set[asyncio.Task] = set()
        self._summary: list[str] = []
        self._output: list[str] = []
        self._output_bytes = 0
        self._active_turn: str | None = None
        self._usage: dict[str, Any] = {}
        self._usage_turn = ""
        self._error = ""
        self._verified = False
        self.cleanup_note = ""
        self.recovery: dict[str, Any] = {}
        from novacode_cli.security.policy import get_policy

        policy = get_policy(Path(model.get("policy_root", request.cwd)))
        public = {
            field.name: getattr(policy, field.name)
            for field in fields(policy)
            if not field.name.startswith("_")
        }
        self.bootstrap = {
            "version": VERSION,
            "agent": request.identity.agent,
            "team": request.identity.team,
            "root": request.identity.root,
            "project": request.identity.project,
            "attempt": uuid.uuid4().hex,
            "provider": model["provider"],
            "model": model["model"],
            "cwd": str(request.cwd),
            "policy": hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest(),
            "agent_type": request.agent_type,
            "auto_approve": request.auto_approve,
            "reasoning_effort": model.get("reasoning_effort"),
            "system_prompt": model.get("system_prompt"),
            "allowed_tools": model.get("allowed_tools", []),
            "required_tools": model.get("required_tools", []),
            "model_options": model.get("model_options", {}),
            "base_url": model.get("base_url"),
            "auth_mode": model.get("auth_mode"),
        }

    async def start(
        self, argv: list[str] | None = None, *, env: dict[str, str] | None = None
    ) -> None:
        """Start the installed Nova implementation, never the worktree's old copy."""
        from novacode_cli.sessions.worktree import _git

        code, revision, _ = await asyncio.to_thread(
            _git, ["rev-parse", "HEAD"], cwd=self.request.cwd
        )
        self.bootstrap["base_revision"] = revision if code == 0 else None
        self.child = await self.supervisor.spawn(
            session_id=self.request.identity.agent,
            name=self.request.agent_type,
            worktree=self.request.cwd,
            argv=argv or [sys.executable, "-m", "novacode_cli.teams.worker"],
            env={**(env or {}), "NOVA_TEAM_ATTEMPT": self.bootstrap["attempt"]},
        )
        if self.request.mode != "remote":
            import psutil

            process_id = self.child.proc.pid
            born = await asyncio.to_thread(lambda: psutil.Process(process_id).create_time())
            self.recovery = {
                "kind": "process",
                "pid": self.child.proc.pid,
                "born": born,
                "attempt": self.bootstrap["attempt"],
            }
        encode_control({"t": "team_bootstrap", **self.bootstrap})
        await self.supervisor._send(self.child, {"t": "team_bootstrap", **self.bootstrap})
        await asyncio.wait_for(asyncio.shield(self.ready), timeout=120)

    async def _message(self, _agent_id: str, message: dict[str, Any]) -> None:  # noqa: PLR0911, PLR0912, PLR0915 - Explicit lifecycle transitions.
        if self.child is None:
            return
        kind = message.get("t")
        if not self._verified and kind not in {"ready", "error", "exited"}:
            if not self.ready.done():
                self.ready.set_exception(TeamError("Worker sent work before a valid handshake"))
            return
        if kind == "ready":
            try:
                validate_handshake(self.bootstrap, message.get("team_handshake", {}))
            except TeamError as exc:
                if not self.ready.done():
                    self.ready.set_exception(exc)
                return
            if not self.ready.done():
                self._verified = True
                self.ready.set_result(True)
        elif kind == "session_request":
            # Never await a parent's approval in the stdout reader: doing so
            # would block further events, cancellation, and approval replies.
            if len(self._rpc_tasks) >= MAX_RPC_REQUESTS:
                await self.supervisor._send(
                    self.child,
                    {
                        "t": "session_reply",
                        "id": message.get("id"),
                        "result": {"error": "Pending team request limit reached"},
                    },
                )
                return
            task = asyncio.create_task(self._rpc(message))
            self._rpc_tasks.add(task)
            task.add_done_callback(self._rpc_tasks.discard)
        elif kind == "ev":
            from novacode_cli import ui_events as ev

            try:
                event = session_protocol.decode_event(message)
            except (TypeError, ValueError):
                self._error = "Invalid worker event; inspect partial work"
                return
            if isinstance(event, ev.AssistantMessage):
                self._summary.append(event.text)
            elif isinstance(event, ev.Error):
                self._error = event.message
        elif kind == "team_output":
            from novacode_cli.teams.output import MAX_OUTPUT_BYTES

            text = message.get("text")
            if (
                message.get("turn_id") != self._active_turn
                or message.get("index") != len(self._output)
                or not isinstance(text, str)
            ):
                self._error = "Invalid result frame; inspect partial work"
                return
            self._output_bytes += len(text.encode("utf-8"))
            if self._output_bytes > MAX_OUTPUT_BYTES:
                self._error = "Result artifact limit exceeded; inspect workspace deliverables"
                return
            self._output.append(text)
        elif kind == "team_usage":
            self._usage = message.get("usage", {})
            self._usage_turn = str(message.get("turn_id", ""))
        elif kind == "turn_done":
            await self.event(
                "idle" if message.get("ok") and not self._error else "failed",
                {
                    "full_output": "".join(self._output)
                    if self._output
                    else "\n".join(self._summary),
                    "summary": "\n".join(self._summary)[-64000:],
                    "error": self._error,
                    "model": f"{self.bootstrap['provider']}:{self.bootstrap['model']}",
                    "usage": self._usage or {"availability": "unavailable"},
                    "usage_turn": self._usage_turn or uuid.uuid4().hex,
                },
            )
            self._active_turn = None
            self._output.clear()
            self._output_bytes = 0
            self._summary.clear()
            self._error = ""
        elif kind == "interrupt":
            # Plans/questions remain the lead's job. Tool calls use team RPC.
            if self.child is not None:
                await self.supervisor._send(
                    self.child,
                    {
                        "t": "interrupt_reply",
                        "id": message["id"],
                        "result": {},
                    },
                )
        elif kind in {"exited", "error"}:
            if not self.ready.done():
                self.ready.set_exception(TeamError("Worker exited before a valid handshake"))
            elif not self.stopped:
                await self.event(
                    "failed", {"error": "Worker exited unexpectedly; inspect partial work"}
                )

    async def _rpc(self, message: dict[str, Any]) -> None:
        try:
            payload = message.get("payload", {})
            encode_control(payload)
            result = await self.gateway(message.get("method", ""), payload)
        except (TeamError, ValueError, TypeError, KeyError) as exc:
            result = {"error": str(exc)}
        if self.child is not None:
            reply = {"t": "session_reply", "id": message["id"], "result": result}
            try:
                encode_control(reply)
            except TeamError:
                reply["result"] = {"error": "Reply exceeds control limit; request a bounded page"}
            await self.supervisor._send(self.child, reply)

    async def send(self, text: str) -> None:
        """Queue explicit text; peer inbox data is injected at the next safe boundary."""
        from novacode_cli.teams.models import Limits

        if self.turns >= Limits().turns:
            message = "Teammate turn limit reached; review and start a fresh team"
            raise TeamError(message)
        self.turns += 1
        self._active_turn = uuid.uuid4().hex
        self._usage = {}
        self._usage_turn = ""
        await self.event("running", {})
        if (
            await self.supervisor.send_prompt(
                self.request.identity.agent,
                text,
                auto_approve=self.request.auto_approve,
                prompt_id=self._active_turn,
            )
            is None
        ):
            msg = "Worker pipe is unavailable"
            raise TeamError(msg)

    async def stop(self) -> bool:
        """Stop, verify process exit, then consider clean worktree cleanup."""
        self.stopped = True
        for task in self._rpc_tasks:
            task.cancel()
        await asyncio.gather(*self._rpc_tasks, return_exceptions=True)
        await self.supervisor.close(self.request.identity.agent)
        if self.child is not None and self.child.proc.returncode is None:
            return False
        if self.worktree is not None:
            self.cleanup_note = await _preserve_or_remove(self.worktree.path, self.worktree.source)
            self.worktree = None
        return True
