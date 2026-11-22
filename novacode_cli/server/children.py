"""Child sessions the socket server can spawn and switch to.

The Textual UI owns session tabs through
:class:`novacode_cli.sessions.supervisor.SessionSupervisor`. Each tab is a full
child Nova process, bound to a workspace, speaking JSONL over stdin/stdout. The
socket server needs the same thing, but its clients speak the
TUI protocol, so this module is the adapter between the two:

* :class:`ChildHost` presents the small host surface the transport already uses
  (``prompt``, ``cancel``, ``interrupt_reply``, ``row``, sinks, ...);
* :class:`ChildSessionManager` owns the supervisor and fans each child frame out
  to the sinks attached to that child.

A child is a **chat** session. Slash commands that live in the parent's
``ServerPort`` are not available inside a child; ``/stop``, ``/status`` and
``/clear`` are the local exceptions. That is preferred to silently forwarding a
command the worker will ignore.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.server import frames
from novacode_cli.server.catalog import CATALOG
from novacode_cli.ui.port import CommandResult, not_ported

if TYPE_CHECKING:
    from collections.abc import Callable

    from novacode_cli.server.sessions import SessionRegistry
    from novacode_cli.sessions.supervisor import ChildSession, SessionSupervisor

logger = logging.getLogger(__name__)

# Local commands a child can answer without the parent's command layer. `stop`
# is a cancellation, `status` is its row, `clear`/`quit` are client actions.
_CHILD_COMMANDS = frozenset({"stop", "status", "clear", "quit", "exit", "q"})


class ChildHost:
    """One spawned child session, in the shape the transport dispatches to."""

    def __init__(
        self,
        child: ChildSession,
        supervisor: SessionSupervisor,
        registry: SessionRegistry,
        *,
        on_gone: Callable[[str], None] | None = None,
    ) -> None:
        """Bind an already-spawned child to a registry and its frame sinks.

        Args:
            child: The supervisor's child record.
            supervisor: Owner used to send prompts and approvals.
            registry: The server registry this host registers in.
            on_gone: Called when the child exits, so the manager can forget it.
        """
        self._child = child
        self._supervisor = supervisor
        self._registry = registry
        self._on_gone = on_gone
        self._sinks: list[Callable[[dict[str, Any]], None]] = []
        self._model: str | None = None
        self._cwd: str | None = str(child.worktree)
        self._status = child.status

    # ── identity ─────────────────────────────────────────────────────────
    @property
    def session_id(self) -> str:
        """The child's session id."""
        return self._child.session_id

    def row(self) -> dict[str, Any]:
        """One `sessions` row, using the model/cwd the child reported."""
        return {
            "id": self.session_id,
            "name": self._child.name,
            "cwd": self._cwd,
            "status": self._status,
            "model": self._model,
        }

    # ── sinks ────────────────────────────────────────────────────────────
    def add_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Send future child frames to *sink*."""
        if sink not in self._sinks:
            self._sinks.append(sink)

    def remove_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Stop sending child frames to *sink*."""
        with contextlib.suppress(ValueError):
            self._sinks.remove(sink)

    def _emit(self, frame: dict[str, Any]) -> None:
        for sink in list(self._sinks):
            with contextlib.suppress(Exception):
                sink(frame)

    # ── inbound frames from the child ────────────────────────────────────
    def feed(self, msg: dict[str, Any]) -> None:
        """Handle one frame the supervisor read from the child.

        Args:
            msg: Decoded child envelope.
        """
        kind = msg.get("t")
        if kind == "ready":
            self._model = msg.get("model") or self._model
            self._cwd = msg.get("cwd") or self._cwd
            self._status = "idle"
            # The client already has this session from the `sessions` row; a
            # second `ready` would look like a reconnect and reset its state.
            return
        if kind == "ev":
            self._status = "running"
        elif kind == "interrupt":
            self._status = "needs-approval"
        elif kind == "turn_done":
            self._status = "idle"
        elif kind == "exited":
            self._status = "exited" if not msg.get("crashed") else "crashed"
            self._emit(
                frames.notice(
                    f"Session {self._child.name} exited.",
                    level="error" if msg.get("crashed") else "info",
                )
            )
            if self._on_gone is not None:
                self._on_gone(self.session_id)
            return
        self._emit(msg)

    # ── host surface used by the transport ───────────────────────────────
    async def prompt(
        self,
        request_id: str,
        text: str,
        *,
        images: Any = None,  # noqa: ANN401 — the worker's image dicts
        auto_approve: Any = None,  # noqa: ANN401 — tri-state from the client
        steer: bool = False,
    ) -> None:
        """Queue a prompt on the child; report a dead child explicitly."""
        # The transport carries image dicts; the supervisor expects objects with
        # `base64_data` and `format`. Convert at this seam rather than widening
        # either side's contract.
        from types import SimpleNamespace

        attached = [
            SimpleNamespace(base64_data=item.get("base64_data"), format=item.get("format"))
            for item in (images or [])
            if isinstance(item, dict) and item.get("base64_data")
        ]
        sent = await self._supervisor.send_prompt(
            self.session_id,
            text,
            images=attached or None,
            auto_approve=auto_approve,
            prompt_id=request_id,
            steer=steer,
        )
        if sent is None:
            self._emit(frames.error("This session is no longer running."))

    async def cancel(self) -> None:
        """Cancel the child's active turn."""
        await self._supervisor.cancel(self.session_id)

    async def interrupt_reply(self, interrupt_id: str, result: Any) -> None:  # noqa: ANN401
        """Forward an approval decision to the child."""
        await self._supervisor.reply_interrupt(self.session_id, interrupt_id, result)

    async def set_approval_mode(self, enabled: Any) -> None:  # noqa: ANN401
        """Set the child's approval mode."""
        if not isinstance(enabled, bool):
            self._emit(frames.error("Invalid approval mode."))
            return
        await self._supervisor.set_auto_approve(self.session_id, enabled=enabled)
        self._emit(frames.notice(f"Approval mode: {'auto' if enabled else 'manual'}"))

    async def command(self, request_id: str, name: str, _args: Any = None) -> None:  # noqa: ANN401
        """Answer the few commands a child owns; refuse the rest explicitly."""
        command = name.strip().lstrip("/").lower()
        if command == "stop":
            await self.cancel()
            result = CommandResult(ok=True, message="Stopping…")
        elif command == "status":
            row = self.row()
            text = (
                f"Session   {row['id']}\nThread    child\nModel     {row['model'] or 'unknown'}\n"
                f"Cwd       {row['cwd']}\nWorkspace {self._child.worktree}"
            )
            result = CommandResult(ok=True, message="Status", blocks=(_block(text),))
        elif command in {"clear", "quit", "exit", "q"}:
            closing = command in {"quit", "exit", "q"}
            result = CommandResult(
                ok=True,
                message="Closing session." if closing else "Transcript cleared.",
                action="quit" if closing else "clear",
            )
        else:
            result = not_ported(name)
        self._emit(
            frames.command_result(
                request_id,
                ok=result.ok,
                message=result.message,
                blocks=result.as_blocks(),
                action=result.action,
            )
        )

    async def list_sessions(self) -> None:
        """Send the shared session list, marking this child active."""
        self._emit(frames.sessions_frame(self._registry.summary(), active=self.session_id))

    async def send_catalogue(self) -> None:
        """Send the global command catalogue."""
        self._emit(
            frames.catalogue(
                [
                    {
                        "name": spec.name,
                        "help": spec.help,
                        "aliases": list(spec.aliases),
                        "wants_text": spec.wants_text,
                        "route": spec.route,
                        "served": spec.served,
                    }
                    for spec in CATALOG.values()
                ]
            )
        )

    async def attach(self, session_id: str) -> None:
        """Explain an attach the transport did not perform for this host."""
        self._emit(
            frames.notice(
                f"Unknown session {session_id}." if session_id else "No session id supplied.",
                level="warning",
            )
        )

    async def resolve_screen(self, _screen_id: str, _value: Any) -> None:  # noqa: ANN401
        """A child has no server-owned screens; ignore a stray answer."""
        return

    async def close(self) -> None:
        """Stop the child process."""
        await self._supervisor.close(self.session_id)


def _block(text: str) -> Any:  # noqa: ANN401 — a Block, imported lazily to keep this module small
    """Build one text block without importing the UI spec at module load."""
    from novacode_cli.ui.port import Block

    return Block("text", text)


class ChildSessionManager:
    """Spawn and close child sessions for one server connection."""

    def __init__(self, registry: SessionRegistry, base_cwd: str | Path | None = None) -> None:
        """Create a manager bound to the registry every host registers in.

        Args:
            registry: The server's shared session registry.
            base_cwd: Workspace new tabs start in; defaults to the server's cwd.
        """
        self._registry = registry
        self._base_cwd = Path(base_cwd or Path.cwd())
        self._hosts: dict[str, ChildHost] = {}
        self._supervisor: SessionSupervisor | None = None

    @property
    def supervisor(self) -> SessionSupervisor:
        """Build the supervisor on first use so a silent server pays nothing."""
        if self._supervisor is None:
            from novacode_cli.sessions.supervisor import SessionSupervisor

            self._supervisor = SessionSupervisor(self._on_message)
        return self._supervisor

    async def _on_message(self, session_id: str, msg: dict[str, Any]) -> None:
        host = self._hosts.get(session_id)
        if host is not None:
            host.feed(msg)

    def _forget(self, session_id: str) -> None:
        host = self._hosts.pop(session_id, None)
        if host is not None:
            self._registry.remove(host)  # type: ignore[arg-type] — duck-typed host surface

    async def create(
        self,
        name: str,
        cwd: str | Path | None = None,
        *,
        resume_id: str | None = None,
    ) -> ChildHost:
        """Spawn one child and register it.

        Args:
            name: Display name for the tab.
            cwd: Workspace; defaults to the manager's base directory.
            resume_id: Saved session to continue; the child starts with
                ``--continue <id>`` and adopts that session's identity.

        Returns:
            The registered host, ready for the caller to attach.

        Raises:
            RuntimeError: When the session limit is reached.
            OSError: When the process cannot start.
        """
        session_id = f"s-{uuid.uuid4().hex[:8]}"
        worktree = Path(cwd or self._base_cwd)
        if resume_id and cwd is None:
            worktree = self._saved_workspace(resume_id, worktree)
        child = await self.supervisor.spawn(
            session_id=session_id,
            name=name.strip() or "session",
            worktree=worktree,
            resume_id=resume_id,
        )
        host = ChildHost(child, self.supervisor, self._registry, on_gone=self._forget)
        self._hosts[session_id] = host
        self._registry.add(host)  # type: ignore[arg-type] — duck-typed host surface
        return host

    @staticmethod
    def _saved_workspace(resume_id: str, fallback: Path) -> Path:
        """The folder a resumed session was saved in, so a tab reopens there.

        `/resume` continues another session's conversation; its tool calls belong
        to the project it was saved in, not to whichever folder spawned the tab.
        A session without a saved project (or one that no longer exists) starts
        in *fallback* rather than failing the spawn.

        Args:
            resume_id: Saved session id the child was asked to continue.
            fallback: Workspace to use when the session names none.

        Returns:
            The workspace the child should start in.
        """
        from novacode_cli.session.session_persistence import SessionManager

        try:
            meta = SessionManager().load_session_meta(resume_id)
        except (OSError, ValueError):
            return fallback
        saved = getattr(meta, "project_root", None)
        if not saved:
            return fallback
        path = Path(str(saved))
        return path if path.is_dir() else fallback

    async def close(self, session_id: str) -> bool:
        """Close one child, unregister it and forget it.

        Args:
            session_id: Child to stop.

        Returns:
            `True` when the child was known.
        """
        host = self._hosts.get(session_id)
        if host is None:
            return False
        self._hosts.pop(session_id, None)
        self._registry.remove(host)  # type: ignore[arg-type]
        with contextlib.suppress(Exception):
            await host.close()
        return True

    async def close_all(self) -> None:
        """Stop every child this connection spawned."""
        for session_id in list(self._hosts):
            await self.close(session_id)
