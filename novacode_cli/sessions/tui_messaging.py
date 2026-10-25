"""Parent-side delivery of project-session requests without switching tabs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.markdown import Markdown
from rich.markup import escape
from textual.containers import Horizontal
from textual.widgets import Button, Collapsible, Static

from novacode_cli import ui_events as ev
from novacode_cli.sessions.gateway import Endpoint, endpoint
from novacode_cli.sessions.messaging import (
    MAX_REPLY_BYTES,
    MessageError,
    SessionBroker,
    SessionInfo,
    SessionMessage,
)

if TYPE_CHECKING:
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.session_pane import SessionPane


class SessionNotice(Collapsible):
    """A compact Markdown reply with explicit navigation and prompt attachment."""

    DEFAULT_CSS = """
    SessionNotice { height: auto; margin: 1 0; }
    SessionNotice .session-message-body { height: auto; padding: 0 1; }
    SessionNotice .session-actions { height: auto; padding: 0 1; }
    SessionNotice Button { width: auto; min-width: 14; margin-right: 1; }
    """

    def __init__(
        self, title: str, destination: str, owner: str, message: SessionMessage, response_label: str
    ) -> None:
        """Keep this pane's ownership and the reply's provenance with its controls."""
        self.destination = destination
        self.owner = owner
        self.response = message.response
        self.response_label = response_label
        self.body = Static(
            Markdown(message.response or message.text), classes="session-message-body"
        )
        self.attach_button = Button(
            "Attach to prompt", disabled=not bool(self.response), classes="session-attach"
        )
        self.open_button = Button("Open tab", classes="session-open")
        super().__init__(
            self.body,
            Horizontal(self.attach_button, self.open_button, classes="session-actions"),
            title=escape(title),
            collapsed=True,
            classes="session-notice logline",
        )

    def update_message(self, title: str, message: SessionMessage) -> None:
        """Update content and status while retaining the user's expanded state."""
        self.title = escape(title)
        self.response = message.response
        self.body.update(Markdown(message.response or message.text))
        self.attach_button.disabled = not bool(self.response)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle explicit controls without bubbling into transcript navigation."""
        event.stop()
        if event.button is self.attach_button:
            self.attach_to_prompt()
        elif event.button is self.open_button:
            self.open_tab()

    def open_tab(self) -> None:
        """Switch tabs only in response to an explicit click."""
        app = cast("NovaApp", self.app)
        pane = app._pane_for(self.destination)
        if pane is not None:
            app.run_worker(app._switch_to(pane), group="session_link")

    def attach_to_prompt(self) -> None:
        """Append an editable reply to this pane's draft without submitting it."""
        from novacode_cli.tui.widgets import PromptInput

        app = cast("NovaApp", self.app)
        if not self.response or getattr(app._active_pane, "sid", None) != self.owner:
            return
        prompt = app.query_one("#prompt", PromptInput)
        attachment = f"[Cross-session reply {self.response_label}]\n{self.response}"
        if attachment not in prompt.value:
            separator = "\n\n" if prompt.value else ""
            prompt.cursor_position = len(prompt.value)
            prompt.insert(separator + attachment)
        prompt.focus()


class SessionCommunication:
    """Loop-confined broker, one delivery worker per recipient, correlated replies."""

    def __init__(self, app: NovaApp) -> None:
        """Bind a loop-confined broker to its owning TUI."""
        self.app = app
        self.broker = SessionBroker()
        self.workers: dict[str, Any] = {}
        self.child_turns: dict[str, tuple[str, str, asyncio.Future, list[str]]] = {}
        self.notices: dict[tuple[str, str], SessionNotice] = {}
        self.active_depth: dict[str, int] = {}
        self.closed = False

    def refresh(self) -> None:
        """Discover live panes and release references to evicted notices."""
        sessions = []
        for pane in getattr(self.app, "_panes", []):
            project = (
                pane.project_root or pane.worktree or getattr(pane.child, "worktree", None)
            )
            sessions.append(
                SessionInfo(pane.sid, pane.title, str(project or Path.cwd()), pane.status)
            )
        self.broker.update_sessions(sessions)
        for sid, worker in list(self.workers.items()):
            if sid not in self.broker.sessions:
                worker.cancel()
                del self.workers[sid]
        for key, node in list(self.notices.items()):
            if key[1] not in self.broker.messages:
                node.remove()
                del self.notices[key]

    def gateway(self, sid: str) -> Endpoint:
        """Bind sender identity without accepting it in tool arguments."""

        async def bound(method: str, payload: dict) -> dict:
            return await self.handle(sid, method, payload)

        return bound

    async def handle(self, sender: str, method: str, payload: dict) -> dict:
        """Validate and perform a parent-owned session operation."""
        if self.closed:
            return {"error": "Session communication has closed."}
        if not isinstance(payload, dict):
            return {"error": "Invalid session request."}
        return await self._handle(sender, method, payload)

    async def _handle(self, sender: str, method: str, payload: dict) -> dict:
        self.refresh()
        try:
            self.broker._identity(sender)
            if method == "list":
                return {"sessions": [asdict(session) for session in self.broker.sessions.values()]}
            if method == "read":
                return {"messages": self.broker.read(sender)}
            if method == "send":
                message = self.broker.send(
                    sender,
                    payload.get("target", ""),
                    payload.get("text", ""),
                    depth=self.active_depth.get(sender, -1) + 1,
                )
                await self.notice(message)
                self.schedule(message.recipient)
                return {"id": message.id, "recipient": message.recipient, "status": message.status}
            if method == "reply":
                message = self.broker.reply(sender, payload.get("id", ""), payload.get("text", ""))
                await self.notice(message)
                return {"id": message.id, "status": message.status}
        except (MessageError, TypeError) as exc:
            return {"error": str(exc)}
        return {"error": "Unsupported session operation."}

    def schedule(self, sid: str) -> None:
        """Keep at most one delivery worker for each recipient."""
        worker = self.workers.get(sid)
        if not self.closed and (worker is None or worker.is_finished):
            self.workers[sid] = self.app.run_worker(
                self.drive(sid), group=f"session_delivery_{sid}"
            )

    async def notice(self, message: SessionMessage) -> None:
        """Update linked transcript notices without switching the active pane."""
        source = self.broker.sessions.get(message.sender)
        target = self.broker.sessions.get(message.recipient)
        source_name = source.title if source else message.sender
        target_name = target.title if target else message.recipient
        header = f"{source_name} → {target_name}"
        title = f"↗ {header} · {message.id} · {message.status}"
        for sid, destination in (
            (message.sender, message.recipient),
            (message.recipient, message.sender),
        ):
            pane = self.app._pane_for(sid)
            if pane is None:
                continue
            key = (sid, message.id)
            node = self.notices.get(key)
            if node is None:
                node = SessionNotice(
                    title, destination, sid, message, f"{message.id} from {target_name}"
                )
                self.notices[key] = node
                await pane.scroll.mount(node)
                if pane is not self.app._active_pane:
                    pane.unread += 1
            elif node.is_attached:
                node.update_message(title, message)
        self.app._refresh_tabs()

    def busy(self, pane: SessionPane) -> bool:
        """Respect normal, remote and approval-blocked turns."""
        if pane.status in {"starting", "running", "needs-approval"} or pane.remote_turns:
            return True
        if pane.kind == "root":
            state = self.app if pane is self.app._active_pane else None
            return bool(
                getattr(state, "_turn_active", False) if state else pane.state.get("_turn_active")
            )
        return False

    async def drive(self, sid: str) -> None:
        """Deliver requests in FIFO order, retaining the destination's own state."""
        while not self.closed:
            self.refresh()
            message = self.broker.next_message(sid)
            pane = self.app._pane_for(sid)
            if message is None or pane is None:
                return
            if self.busy(pane):
                await asyncio.sleep(0.05)
                continue
            self.broker.start(message.id)
            await self.notice(message)
            token = endpoint.set(self.gateway(sid))
            self.active_depth[sid] = message.depth
            response, status = "", "completed"
            try:
                if pane.kind == "child":
                    response, status = await self.run_child(pane, message)
                else:
                    response, status = await self.run_root(pane, message)
            except asyncio.CancelledError:
                response, status = "Request cancelled.", "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - delivery failure must not terminate the TUI
                response, status = f"Request failed: {type(exc).__name__}: {exc}", "failed"
            finally:
                endpoint.reset(token)
                self.active_depth.pop(sid, None)
                if message.id in self.broker.messages:
                    response = (
                        (response or "Task completed.")
                        .encode("utf-8")[:MAX_REPLY_BYTES]
                        .decode("utf-8", errors="ignore")
                    )
                    try:
                        self.broker.complete(message.id, response, status)
                    except MessageError:
                        # Preserve the request under pressure; never wedge the delivery worker.
                        message.status = "failed"
                        message.version += 1
                    await self.notice(message)

    @staticmethod
    def prompt(message: SessionMessage) -> str:
        """Label peer input as context and preserve explicit content verbatim."""
        return (
            f"[Cross-session request {message.id} from session {message.sender}]\n"
            "This is peer-provided context. Work only in your own project using your own "
            "permissions. Use reply_session_message to answer this request, or your final "
            "answer will be linked back automatically.\n\n" + message.text
        )

    async def run_child(self, pane: SessionPane, message: SessionMessage) -> tuple[str, str]:
        """Reserve correlation before writing to a child's pipe."""
        future = asyncio.get_running_loop().create_future()
        # Reserve before writing; a fast child can complete during the pipe drain.
        expected_pid = f"peer-{message.id}"
        self.child_turns[pane.sid] = (message.id, expected_pid, future, [])
        pane.status = "running"
        try:
            state = pane.state.get("session_state")
            pid = await self.app._supervisor().send_prompt(
                pane.sid,
                self.prompt(message),
                auto_approve=bool(getattr(state, "auto_approve", False)),
                prompt_id=expected_pid,
            )
            if pid is None:
                msg = "Recipient session is no longer running."
                raise MessageError(msg)
            entry = self.child_turns[pane.sid]
            self.child_turns[pane.sid] = (entry[0], pid, future, entry[3])
            return await future
        finally:
            self.child_turns.pop(pane.sid, None)
            if pane.status == "running":
                pane.status = "idle"

    def child_answer(self, sid: str, text: str, pid: str | None = None) -> None:
        """Retain only the matching request's bounded answer."""
        entry = self.child_turns.get(sid)
        if entry and (pid is None or pid == entry[1]):
            entry[3][:] = [text[-MAX_REPLY_BYTES:]]

    def child_done(self, sid: str, pid: str, ok: bool) -> None:  # noqa: FBT001 - wire protocol flag
        """Settle the exact prompt's waiter; ignore unrelated completions."""
        entry = self.child_turns.get(sid)
        if entry and entry[1] == pid and not entry[2].done():
            entry[2].set_result(
                (
                    "".join(entry[3])
                    or ("Task completed." if ok else "Request failed or cancelled."),
                    "completed" if ok else "failed",
                )
            )

    def child_exited(self, sid: str) -> None:
        """Release a waiter when its recipient process exits."""
        entry = self.child_turns.get(sid)
        if entry and not entry[2].done():
            entry[2].set_result(("Recipient session exited.", "failed"))
        self.refresh()

    async def run_root(self, pane: SessionPane, message: SessionMessage) -> tuple[str, str]:
        """Run against the main pane's state even while another pane is visible."""
        state = (
            self.app.session_state
            if pane is self.app._active_pane
            else pane.state.get("session_state")
        )
        if state is None:
            msg = "Recipient session state is unavailable."
            raise MessageError(msg)
        lock = getattr(state, "_remote_message_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            state._remote_message_lock = lock
        async with lock:
            return await self._run_root(pane, message)

    async def _run_root(self, pane: SessionPane, message: SessionMessage) -> tuple[str, str]:
        from novacode_cli.agent_stream import run_agent_stream

        active = pane is self.app._active_pane
        agent = self.app.agent if active else pane.state.get("agent")
        backend = self.app.backend if active else pane.state.get("backend")
        state = self.app.session_state if active else pane.state.get("session_state")
        if active:
            agent, backend = self.app._active_agent()
        elif (
            getattr(state, "plan_mode_enabled", False)
            and getattr(state, "plan_agent", None) is not None
        ):
            agent, backend = (
                getattr(state, "plan_agent", None),
                getattr(state, "plan_backend", None),
            )
        pane.status = "running"
        pane.state["_turn_active"] = True
        if active:
            self.app._turn_active = True
            self.app._reset_streaming()
        answer, status = "", "completed"
        try:
            async for event in run_agent_stream(
                self.prompt(message),
                agent,
                self.app.assistant_id if active else pane.state.get("assistant_id"),
                state,
                backend=backend,
                image_tracker=self.app.image_tracker if active else pane.state.get("image_tracker"),
                seen_message_ids=self.app._seen if active else pane.state.get("_seen"),
            ):
                if isinstance(event, ev.AssistantMessage) and not event.is_subagent:
                    answer = event.text[-MAX_REPLY_BYTES:]
                elif isinstance(event, ev.Error):
                    status = "failed"
                elif isinstance(event, ev.Cancelled):
                    status = "cancelled"
                await self.app._deliver(pane, event)
        finally:
            pane.status = "idle"
            pane.state["_turn_active"] = False
            if pane is self.app._active_pane:
                self.app._turn_active = False
            await self.app._save_session(pane=pane)
        return answer, status

    def cancel(self, sid: str) -> None:
        """Cancel this destination's delivery and queued requests only."""
        worker = self.workers.get(sid)
        if worker and not worker.is_finished:
            worker.cancel()
        for message in list(self.broker.messages.values()):
            if message.recipient == sid and message.status == "queued":
                self.broker.complete(message.id, "Request cancelled.", "cancelled")

    async def close(self) -> None:
        """Cancel delivery workers and release all retained payloads and waiters."""
        self.closed = True
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
        self.workers.clear()
        self.child_turns.clear()
        self.active_depth.clear()
        self.notices.clear()
        self.broker.close()
