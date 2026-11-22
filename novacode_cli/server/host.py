"""One live session behind a socket connection.

``SessionHost`` is what the Textual TUI is today: it owns the agent, the session
state, the active turn, pending human-in-the-loop decisions and the command
surface. What it does *not* own is a terminal — every frame goes out through an
``emit`` callable supplied by the transport.

The turn machinery is reused, not reimplemented: ``sessions/worker.py` already
encodes the hard rules (one turn at a time, live steering, interrupts resolved in
a ``finally`` so a dead peer can never wedge the graph, a save on every exit
path). The host drives the same :class:``SessionWorker`` the parallel-session
child drives, with the socket as its emitter, and adds the three things the
stdio child has no need for: a ``ready`` handshake, slash commands routed through
:class:``novacode_cli.ui.port_server.ServerPort``, and a session list.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from novacode_cli.server import frames
from novacode_cli.server.sessions import SessionCapacityError, SessionRegistry
from novacode_cli.sessions.supervisor import MAX_SESSIONS
from novacode_cli.sessions.worker import SessionWorker
from novacode_cli.ui.port_server import ServerPort

if TYPE_CHECKING:
    from collections.abc import Callable

    from novacode_cli.ui.port import ScreenSpec

    # Where one client's frames go; a session may have several (see `add_sink`).
    FrameSink = Callable[[dict[str, Any]], None]

logger = logging.getLogger(__name__)

# Same ceiling as the stdio child: further prompts are refused explicitly rather
# than queued without bound.
MAX_QUEUED_PROMPTS = 16

# Sessions one server may hold. Reused from the supervisor instead of redefining
# a second, drifting number; increment 1 serves exactly one per connection and
# reports the ceiling so a client can say why creating another failed.
SESSION_CAPACITY = MAX_SESSIONS

# How long a command waits for a client to answer a `screen` frame. A client
# that dies mid-modal must not wedge the command coroutine: the wait fails
# closed to `None`, which every caller documents as its default answer.
SCREEN_TIMEOUT = 600.0


@dataclass
class SessionSpec:
    """Everything a host needs to serve one session.

    Attributes:
        agent: Built LangGraph agent.
        session_state: ``SessionState`` for this session.
        assistant_id: Agent identifier used for memory and prompts.
        backend: Composite filesystem backend, or ``None`.
        model_name: Model name for status and token accounting.
        session_manager: Persistence manager, or ``None` for an unsaved session.
        name: Display name for the session list.
        session_id: Persisted session id, or ``None`.
        cwd: Working directory shown to the client.
        sandbox_id: Sandbox the session is attached to, forwarded to the shared
            command layer so a sandboxed handler behaves as it does in Textual.
        sandbox_type: Sandbox kind, e.g. ``"local"`` or ``"docker"``.
    """

    agent: Any
    session_state: Any
    assistant_id: str = "nova-agent"
    backend: Any = None
    model_name: str | None = None
    session_manager: Any = None
    name: str = "nova"
    session_id: str | None = None
    cwd: str | None = None
    sandbox_id: str | None = None
    sandbox_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _screen_body(spec: Any) -> dict[str, Any] | None:  # noqa: ANN401 — a UI spec, kept untyped here
    """Render a screen spec as the body a `command_result` frame carries.

    Args:
        spec: A `novacode_cli.ui.port.ScreenSpec`, or `None`.

    Returns:
        `{"kind", "title", "payload"}`, or `None` when there is no screen.
    """
    if spec is None:
        return None
    return {"kind": spec.kind, "title": spec.title, "payload": spec.payload}


class SessionHost:
    """Serve exactly one session to one connection."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        spec_factory: Callable[[], Any],
        *,
        registry: SessionRegistry | None = None,
    ) -> None:
        """Wire a host to a frame sink and a lazily built session.

        Args:
            emit: Sink for outbound frames (the transport's non-blocking send).
            spec_factory: Builds the session on :meth:`start`, so the client can
                paint before an agent exists.
            registry: The server's session registry, when this host should be
                reachable by id. Without it a host serves only its own
                connection, which is what a test double wants.
        """
        self._sinks: list[FrameSink] = [emit]
        self._spec_factory = spec_factory
        self._registry = registry
        self._worker: SessionWorker | None = None
        self._port: ServerPort | None = None
        self.spec: SessionSpec | None = None
        self._turn: asyncio.Task[None] | None = None
        self._stop = False
        # Outstanding `screen` frames, keyed by the id the client must quote.
        # Every entry is resolved (or failed closed) in :meth:`close`.
        self._screens: dict[str, asyncio.Future[Any]] = {}
        self._screen_seq = 0

    def _emit_frame(self, msg: dict[str, Any]) -> None:
        """Deliver one frame to every client watching this session.

        A session starts with the client that created it. `attach` adds a
        second viewer, so a turn that arrives while another client is attached
        must reach both: dropping one would make the transcript disagree with
        itself depending on who asked.

        Args:
            msg: Envelope to send.
        """
        for sink in list(self._sinks):
            sink(msg)

    def add_sink(self, sink: FrameSink) -> None:
        """Start delivering this session's frames to another client.

        Args:
            sink: The attaching connection's send callable.
        """
        if sink not in self._sinks:
            self._sinks.append(sink)

    def remove_sink(self, sink: FrameSink) -> None:
        """Stop delivering this session's frames to one client.

        Args:
            sink: The callable to forget. Attaching twice is harmless.
        """
        with contextlib.suppress(ValueError):
            self._sinks.remove(sink)

    def use_registry(self, registry: SessionRegistry | None) -> None:
        """Make this host reachable by id through *registry* before it starts.

        Args:
            registry: The server's registry, or `None` to keep this host private
                to its connection (what a test double wants).
        """
        self._registry = registry

    # ── identity ─────────────────────────────────────────────────────────
    @property
    def session_id(self) -> str | None:
        """The id this session is keyed by, or `None` before `start`.

        Returns:
            The session id the client knows, falling back to `"local"` for a
            session that was never saved, which is what the transport reports.
        """
        spec = self.spec
        if spec is None:
            return None
        return spec.session_id or getattr(spec.session_state, "session_id", None) or "local"

    def row(self) -> dict[str, Any]:
        """Build one `sessions` row for this host.

        Returns:
            `{id, name, cwd, status, model}`, as `frames.sessions_frame` expects.
        """
        spec = self.spec
        if spec is None:
            return {"id": "", "name": "", "cwd": None, "status": "idle", "model": None}
        return {
            "id": self.session_id or "local",
            "name": spec.name,
            "cwd": spec.cwd,
            "status": "running" if self._turn_running() else "idle",
            "model": spec.model_name,
        }

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Build the session and announce it with a ``ready`` frame."""
        spec = self._spec_factory()
        if asyncio.iscoroutine(spec):
            spec = await spec
        self.spec = spec
        if self._registry is not None:
            # Before `ready`: a client that lists sessions the moment it is told
            # the session is live must see itself in that list, and a full server
            # must refuse here rather than after the client thought it succeeded.
            try:
                self._registry.add(self)
            except SessionCapacityError:
                logger.warning("refused a session: registry full")
                self._emit_frame(
                    frames.error(
                        "This server already holds the maximum number of sessions.",
                        code="capacity",
                    )
                )
                self._stop = True
                raise
        self._worker = SessionWorker(
            agent=spec.agent,
            assistant_id=spec.assistant_id,
            session_state=spec.session_state,
            backend=spec.backend,
            model_name=spec.model_name,
            session_manager=spec.session_manager,
            emit=self._emit_frame,
        )
        self._port = ServerPort(
            self._emit_frame,
            self._worker,
            cwd=spec.cwd,
            ask_screen=self._ask_screen,
            sandbox_id=spec.sandbox_id,
            sandbox_type=spec.sandbox_type,
            session_rows=self._session_rows,
            busy=self._turn_running,
        )
        self._emit_frame(
            frames.ready(
                session_id=spec.session_id or getattr(spec.session_state, "session_id", None),
                thread_id=getattr(spec.session_state, "thread_id", None),
                cwd=spec.cwd,
                model=spec.model_name,
                assistant_id=spec.assistant_id,
                ui="ratatui",
                capacity=SESSION_CAPACITY,
                **spec.extra,
            )
        )

    async def close(self) -> None:
        """Cancel the turn, finish queued prompts and save the session."""
        if self._stop:
            return
        self._stop = True
        if self._registry is not None:
            # Forget this id before the worker is saved: a session that is gone
            # must not appear in another client's list while it shuts down.
            self._registry.remove(self)
        self._fail_screens()
        worker = self._worker
        if worker is None:
            return
        with contextlib.suppress(Exception):
            await self.cancel()
        worker._cancel_queued()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(worker._save(), timeout=5)
        if self._port is not None:
            with contextlib.suppress(Exception):
                await self._port.close()

    # ── prompts ──────────────────────────────────────────────────────────
    async def prompt(
        self,
        prompt_id: str,
        text: str,
        *,
        images: Any = None,  # noqa: ANN401 — decoded per image below
        auto_approve: Any = None,  # noqa: ANN401 — validated to bool below
        steer: bool = False,
    ) -> None:
        """Admit one prompt: run it, queue it, or refuse it explicitly.

        Args:
            prompt_id: Client-chosen id echoed on every frame for this turn.
            text: Prompt text. Slash text is handled as a command instead.
            images: List of ``ImageData`` dicts, if any.
            auto_approve: Approval mode for this prompt, or ``None` to keep the
                session's current mode.
            steer: Deliver as a live steering instruction to a running turn.
        """
        from novacode_cli.image_utils import ImageData

        worker = self._require_worker()
        state = worker.session_state
        try:
            image_objs = [ImageData(**image) for image in (images or [])]
        except (TypeError, ValueError):
            self._emit_frame(frames.error("Invalid prompt settings or image attachments."))
            self._emit_frame(frames.turn_done(prompt_id, ok=False))
            return
        approve = state.auto_approve if auto_approve is None else auto_approve
        if not isinstance(approve, bool):
            self._emit_frame(frames.error("Invalid approval mode."))
            self._emit_frame(frames.turn_done(prompt_id, ok=False))
            return

        if image_objs and not text.strip():
            text = "Please describe this image."
        if not text.strip():
            self._emit_frame(frames.turn_done(prompt_id, ok=True))
            return
        if text.lstrip().startswith("/") and not image_objs:
            await self.run_slash(prompt_id, text)
            return

        if self._turn_running():
            if len(worker._queued) >= MAX_QUEUED_PROMPTS:
                self._emit_frame(frames.error("Prompt queue is full."))
                self._emit_frame(frames.turn_done(prompt_id, ok=False))
                return
            if steer and approve == state.auto_approve:
                self._steer(prompt_id, text, approve=approve)
            else:
                worker._queued.append((prompt_id, text, image_objs, approve))
            self._emit_frame(frames.notice("Queued behind the running turn."))
            return

        self._start(prompt_id, text, image_objs, approve=approve)

    def _steer(self, prompt_id: str, text: str, *, approve: bool) -> None:
        """Fold a live follow-up into the running turn.

        Args:
            prompt_id: Client prompt id.
            text: Instruction text.
            approve: Approval mode this prompt was sent under.
        """
        from novacode_cli.bootstrap.steering import SteeringInstruction
        from novacode_cli.tools.waiter_tools import interrupt_task_waits

        worker = self._require_worker()
        instruction = SteeringInstruction("steer", text, live=True)
        worker.session_state.steering_instructions.append(instruction)
        worker._live_prompts[prompt_id] = (instruction, approve)
        interrupt_task_waits(worker.session_state.thread_id)

    def _start(self, prompt_id: str, text: str, images: list[Any], *, approve: bool) -> None:
        """Start a turn and remember the task so it can be cancelled.

        Args:
            prompt_id: Client prompt id.
            text: Prompt text.
            images: Decoded images.
            approve: Approval mode for the turn.
        """
        self._turn = asyncio.create_task(self._run(prompt_id, text, images, approve=approve))

    async def _run(self, prompt_id: str, text: str, images: list[Any], *, approve: bool) -> None:
        """Run one turn through the worker, then start whatever was queued.

        Args:
            prompt_id: Client prompt id.
            text: Prompt text.
            images: Decoded images.
            approve: Approval mode for the turn.
        """
        worker = self._require_worker()
        try:
            await worker._run_turn(prompt_id, text, images, approve)
        finally:
            self._turn = None
            self._drain_queue()

    def _drain_queue(self) -> None:
        """Start the next queued prompt once the current turn is finished."""
        worker = self._worker
        if worker is None or self._stop or not worker._queued:
            return
        prompt_id, text, images, approve = worker._queued.popleft()
        self._start(prompt_id, text, images, approve=approve)

    def _turn_running(self) -> bool:
        """Report whether a turn is currently executing.

        Returns:
            ``True`` while a turn task is alive.
        """
        return self._turn is not None and not self._turn.done()

    async def cancel(self) -> None:
        """Cancel the active turn, killing a foreground shell command first."""
        worker = self._worker
        if worker is None or not self._turn_running():
            return
        with contextlib.suppress(Exception):
            from novacode_cli.shell.jobs import request_kill

            request_kill()
        if self._turn is not None:
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn

    # ── screens ──────────────────────────────────────────────────────────
    async def _ask_screen(self, spec: ScreenSpec) -> Any:  # noqa: ANN401 — a modal owns its shape
        """Present a modal through the client and wait for its answer.

        Args:
            spec: The screen the command asked for.

        Returns:
            The client's answer, or `None` when the client did not answer within
            :data:`SCREEN_TIMEOUT` (the calling handler's documented default).
        """
        self._screen_seq += 1
        screen_id = f"scr-{self._screen_seq}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._screens[screen_id] = future
        self._emit_frame(
            frames.screen(screen_id, kind=spec.kind, title=spec.title, payload=spec.payload)
        )
        try:
            return await asyncio.wait_for(future, timeout=SCREEN_TIMEOUT)
        except TimeoutError:
            self._emit_frame(frames.notice(f"The {spec.kind} screen timed out.", level="warning"))
            return None
        finally:
            self._screens.pop(screen_id, None)

    def _fail_screens(self) -> None:
        """Resolve every outstanding modal with its fail-closed default."""
        for future in self._screens.values():
            if not future.done():
                future.set_result(None)
        self._screens.clear()

    async def resolve_screen(self, screen_id: str, result: Any) -> None:  # noqa: ANN401 — per-kind shape
        """Resolve a pending modal with the client's answer.

        Args:
            screen_id: The `screen` frame's id.
            result: The answer, in the shape that screen kind documents.
        """
        future = self._screens.get(screen_id)
        if future is None or future.done():
            self._emit_frame(frames.notice("That screen is no longer open.", level="warning"))
            return
        future.set_result(result)

    async def interrupt_reply(self, request_id: str, result: Any) -> None:  # noqa: ANN401
        """Resolve a pending human-in-the-loop request.

        Args:
            request_id: The ``interrupt`` frame's id.
            result: The client's decision, in the shape the interrupt kind wants.
        """
        worker = self._worker
        if worker is None:
            return
        future = worker._pending.get(request_id)
        if future is None or future.done():
            self._emit_frame(frames.notice("That approval is no longer pending.", level="warning"))
            return
        future.set_result(result)

    # ── commands ─────────────────────────────────────────────────────────
    async def run_slash(self, prompt_id: str, text: str) -> None:
        """Run slash text sent as a prompt, answering with ``command_result``.

        Args:
            prompt_id: Client prompt id, used as the result's id.
            text: The raw slash text, e.g. ``"/model gpt-5"``.
        """
        parts = text.strip()[1:].split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else None
        await self.command(prompt_id, name, args)
        self._emit_frame(frames.turn_done(prompt_id, ok=True))

    async def command(self, request_id: str, name: str, args: Any = None) -> None:  # noqa: ANN401
        """Run one command through the session's port.

        Args:
            request_id: The client ``command`` frame's id.
            name: Command name, with or without a leading slash.
            args: Raw argument text.
        """
        port = self._port
        if port is None:
            self._emit_frame(frames.error("session is not ready", code="state"))
            return
        result = await port.run(name, args if isinstance(args, str) else None)
        self._emit_frame(
            frames.command_result(
                request_id,
                ok=result.ok,
                message=result.message,
                blocks=result.as_blocks(),
                action=result.action,
                prompt=result.prompt,
                screen=_screen_body(result.screen),
                attach=result.attach,
                create=result.create,
                resume=result.resume,
                close=result.close,
            )
        )
        if result.prompt:
            # The handler answered with a prompt instead of printing (`/init`,
            # `/goal`, `/skill:x`), exactly as the Textual UI's
            # `_stream_prompt` does: start the turn it asked for.
            await self.prompt(f"{request_id}:prompt", result.prompt)

    async def list_sessions(self) -> None:
        """Answer a ``sessions`` frame with every session the server holds."""
        if self._registry is not None and len(self._registry) > 0:
            self._emit_frame(
                frames.sessions_frame(self._registry.summary(), active=self.session_id)
            )
            return
        spec = self.spec
        if spec is None:
            self._emit_frame(frames.sessions_frame([], active=None))
            return
        self._emit_frame(frames.sessions_frame([self.row()], active=self.session_id))

    def _session_rows(self) -> list[dict[str, Any]]:
        """Rows for a session-list screen, from the same registry `sessions` uses."""
        if self._registry is not None and len(self._registry) > 0:
            return self._registry.summary()
        return [self.row()] if self.spec is not None else []

    async def send_catalogue(self) -> None:
        """Answer a `catalogue` frame with every command the client may offer."""
        from novacode_cli.server.catalog import CATALOG

        self._emit_frame(
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
        """Attach the connection to another session.

        Args:
            session_id: Requested session id.
        """
        current = getattr(self.spec.session_state, "session_id", None) if self.spec else None
        if session_id and session_id == (self.spec.session_id or current):
            self._emit_frame(frames.notice("Already attached to that session."))
            return
        self._emit_frame(
            frames.notice(
                "Attaching to another session needs the session-tab wave; "
                "this connection serves one session.",
                level="warning",
            )
        )

    async def set_approval_mode(self, enabled: Any) -> None:  # noqa: ANN401
        """Set the session's approval mode.

        Args:
            enabled: Must be a bool; anything else is refused explicitly.
        """
        worker = self._worker
        if worker is None:
            return
        if not isinstance(enabled, bool):
            self._emit_frame(frames.error("Invalid approval mode."))
            return
        worker.session_state.auto_approve = enabled
        self._emit_frame(frames.notice(f"Approval mode: {'auto' if enabled else 'manual'}"))

    # ── internals ────────────────────────────────────────────────────────
    def _require_worker(self) -> SessionWorker:
        """Return the live worker.

        Returns:
            The worker created by :meth:`start`.

        Raises:
            RuntimeError: When called before :meth:`start`.
        """
        if self._worker is None:
            msg = "session host used before start()"
            raise RuntimeError(msg)
        return self._worker
