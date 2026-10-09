"""Public, persistent JSON-lines interface for local remote-app bridges."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import itertools
import json
import math
import os
import sys
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict

from novacode_cli import ui_events as ev
from novacode_cli.core.agent_loop import default_interrupt_response
from novacode_cli.headless.input import MAX_PROMPT_SIZE
from novacode_cli.headless.output import HeadlessOutput
from novacode_cli.sessions import protocol
from novacode_cli.sessions.worker import SessionWorker
from novacode_cli.ui.hitl_approval import evaluate_tool_actions


def _reject_constant(value):
    raise ValueError("JSON constants must be finite")


def _finite_json(value):
    """Tool/provider metadata may contain NaN; keep the public wire valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    return value


class PipeSession(SessionWorker):
    """Reuse worker lifecycle while exposing a separate versioned public contract."""

    MAX_PENDING = 16
    HISTORY = 256

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.instance_id = getattr(self.session_state, "pipe_instance_id", None) or uuid.uuid4().hex
        self._sequence = itertools.count(1)
        self._output = HeadlessOutput(
            "stream-json",
            self.session_state.session_id,
            self.model_name,
            fd=getattr(self.session_state, "headless_out_fd", None),
        )
        self._output_lock = threading.Lock()
        self._emit = self._frame
        self._requests = OrderedDict()
        self._public_interrupts = {}
        self._policy = []
        self._terminal = None
        self._status = "idle"
        self._completed = False
        self._plain_ids = itertools.count(1)
        self.inbox = asyncio.Queue(maxsize=32)
        self._loop = None
        self._reader = None
        self._output_closed = False
        self.session_state._pipe_emit = self.publish

    def publish(self, type_, **fields):
        with self._output_lock:
            record = {
                "type": type_,
                "instance_id": self.instance_id,
                "session_id": self.session_state.session_id,
                "sequence": next(self._sequence),
                **fields,
            }
            try:
                self._output._writeln(_finite_json(protocol.jsonable(record)))
            except (OSError, ValueError):
                self._output_closed = True
                # A disconnected controller must not leave an autonomous turn.
                if self._loop and not self._stop:
                    with contextlib.suppress(RuntimeError):
                        self._loop.call_soon_threadsafe(self._disconnect)

    def _disconnect(self):
        self._stop = True
        if self._turn_running():
            self._turn.cancel()
        with contextlib.suppress(asyncio.QueueFull):
            self.inbox.put_nowait(None)

    def _frame(self, msg):
        kind = msg.get("t")
        if kind == "ready":
            self.session_state.pipe_ready = True
            timer = getattr(self.session_state, "_pipe_startup_timer", None)
            if timer:
                timer.cancel()
            self.publish(
                "ready",
                protocol="nova.pipe",
                protocol_version=1,
                cwd=os.getcwd(),
                model=self.model_name,
                provider=getattr(self.session_state, "session_model_provider", None),
                capabilities=[
                    "prompt",
                    "cancel",
                    "approval",
                    "interrupt_reply",
                    "ping",
                    "shutdown",
                ],
                max_pending=self.MAX_PENDING,
            )
        elif kind == "interrupt":
            public_id = f"{self.instance_id}:{msg['id']}"
            self._public_interrupts[public_id] = (msg["id"], msg["kind"], list(self._policy))
            self.publish(
                "approval_required" if msg["kind"] == "tool" else "input_required",
                request_id=self._turn_id,
                interrupt_id=public_id,
                kind=msg["kind"],
                payload=msg["payload"],
            )
        elif kind == "turn_done":
            self._terminal = msg
        elif kind == "ev":
            event = protocol.decode_event(msg)
            fields = {"request_id": self._turn_id}
            if isinstance(event, ev.TextDelta):
                self.publish("text_delta", content=event.text, **fields)
            elif isinstance(event, ev.TextDiscard):
                self.publish("text_discard", **fields)
            elif isinstance(event, ev.AssistantMessage):
                self.publish(
                    "text",
                    content=event.text,
                    agent=event.agent_name,
                    is_subagent=event.is_subagent,
                    **fields,
                )
            elif isinstance(event, ev.ToolCall):
                self.publish(
                    "tool_call",
                    tool=event.name,
                    args=event.args,
                    tool_call_id=event.call_id,
                    **fields,
                )
            elif isinstance(event, ev.ToolResult):
                self.publish(
                    "tool_result",
                    output=event.full_output or event.preview,
                    tool_call_id=event.call_id,
                    is_error=event.is_error,
                    **fields,
                )
            elif isinstance(event, ev.Error):
                if self._status == "running":
                    self._status = "error"
                self.publish("error", code="execution_error", message=event.message, **fields)
            elif isinstance(event, ev.Cancelled):
                self._status = "cancelled"
            elif isinstance(event, ev.Done):
                self._completed = True
            elif isinstance(event, ev.UsageUpdate):
                self.publish("usage", usage=asdict(event), **fields)
        elif kind == "jobs":
            self.publish("tasks", tasks=msg["jobs"])
        elif kind == "notification":
            self.publish("notification", notification=msg["notification"])
        elif kind == "error":
            self.publish("error", code="worker_error", message=msg.get("message", "Worker error"))

    async def _stdin_pump(self):
        # A daemon reader avoids asyncio's default executor waiting forever on
        # Windows readline after an explicit shutdown with stdin still open.
        loop = asyncio.get_running_loop()
        source = getattr(sys.stdin, "buffer", sys.stdin)

        def reader():
            while not self._stop:
                try:
                    raw = source.readline(MAX_PROMPT_SIZE + 1)
                    if len(raw) > MAX_PROMPT_SIZE:
                        message = {
                            "type": "_input_error",
                            "fatal": True,
                            "message": "Input frame exceeds 1 MiB",
                        }
                    elif not raw:
                        message = None
                    else:
                        try:
                            text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
                            if getattr(self.session_state, "pipe_input_format", "jsonl") == "text":
                                message = {
                                    "type": "prompt",
                                    "id": f"line-{next(self._plain_ids)}",
                                    "content": text.rstrip("\r\n"),
                                }
                            else:
                                message = json.loads(text, parse_constant=_reject_constant)
                                if not isinstance(message, dict):
                                    raise ValueError("Expected a JSON object")
                                if message.get("type") == "_input_error":
                                    raise ValueError("Reserved input frame")
                        except (ValueError, UnicodeError, RecursionError):
                            message = {
                                "type": "_input_error",
                                "message": "Expected a UTF-8 JSON object",
                            }
                    future = asyncio.run_coroutine_threadsafe(self.inbox.put(message), loop)
                    while not self._stop:
                        try:
                            future.result(timeout=0.2)
                            break
                        except TimeoutError:
                            continue
                    if self._stop:
                        future.cancel()
                        return
                    if message is None or (isinstance(message, dict) and message.get("fatal")):
                        return
                except (OSError, ValueError, RuntimeError, concurrent.futures.CancelledError):
                    if not self._stop and not loop.is_closed():
                        pending = self.inbox.put(None)
                        try:
                            asyncio.run_coroutine_threadsafe(pending, loop)
                        except RuntimeError:
                            pending.close()
                    return

        self._reader = threading.Thread(target=reader, name="nova-pipe-input", daemon=True)
        self._reader.start()
        await asyncio.Future()

    def _reject(self, code, message, request_id=None):
        self.publish("error", code=code, message=message, request_id=request_id)

    def _finish(self, pid, status):
        done = {"request_id": pid, "status": status, "ok": status == "success"}
        entry = self._requests.get(pid)
        if entry:
            entry["done"] = done
        self.publish("done", **done)

    def translate(self, msg):
        """Validate a public frame before it can reach the private worker."""
        if msg is None:
            self._cancel_queued()
            return None
        if self._stop:
            self._cancel_queued()
            return {"t": "shutdown"}
        if not isinstance(msg, dict):
            self._reject("invalid_input", "Expected a JSON object")
            return {}
        kind = msg.get("type")
        if kind == "_input_error":
            self._reject("invalid_input", msg["message"])
            return {"t": "shutdown"} if msg.get("fatal") else {}
        if kind == "prompt":
            pid, content = msg.get("id"), msg.get("content")
            if not isinstance(pid, str) or not pid.strip() or len(pid) > 128:
                self._reject(
                    "invalid_request", "Prompt requires a non-empty id (max 128 characters)"
                )
                return {}
            try:
                encoded = content.encode("utf-8") if isinstance(content, str) else b""
                pid.encode("utf-8")
            except UnicodeError:
                encoded = b""
            if not encoded or not content.strip() or len(encoded) > MAX_PROMPT_SIZE:
                self._reject(
                    "invalid_request", "Prompt requires non-empty UTF-8 content (max 1 MiB)", pid
                )
                return {}
            if any(key in msg for key in ("auto_approve", "cwd", "model", "images")):
                self._reject(
                    "invalid_request",
                    "Runtime settings are fixed at process startup; image input is not supported yet",
                    pid,
                )
                return {}
            digest = hashlib.sha256(encoded).hexdigest()
            existing = self._requests.get(pid)
            if existing:
                if existing["digest"] != digest:
                    self._reject(
                        "request_id_conflict", "This id was already used for different content", pid
                    )
                elif existing.get("done"):
                    self.publish("done", **existing["done"], replayed=True)
                else:
                    self.publish("accepted", request_id=pid, duplicate=True)
                return {}
            if len(self._queued) >= self.MAX_PENDING:
                self._reject("queue_full", "Pending prompt queue is full", pid)
                return {}
            if len(self._requests) >= self.HISTORY:
                completed = next(
                    (key for key, entry in self._requests.items() if entry.get("done")), None
                )
                if completed is not None:
                    self._requests.pop(completed)
            self._requests[pid] = {"digest": digest}
            self.publish("accepted", request_id=pid, duplicate=False)
            return {
                "t": "prompt",
                "id": pid,
                "text": content,
                "auto_approve": self.session_state.auto_approve,
            }
        if kind in ("approval", "interrupt_reply"):
            iid = msg.get("interrupt_id")
            entry = self._public_interrupts.get(iid) if isinstance(iid, str) else None
            if not entry or entry[0] not in self._pending or self._pending[entry[0]].done():
                self._reject(
                    "stale_interrupt", "Interrupt is unknown, expired, or already answered"
                )
                return {}
            internal_id, request_kind, policy = entry
            if request_kind == "tool":
                if kind != "approval" or msg.get("decision") not in ("approve", "reject"):
                    self._reject(
                        "invalid_approval", "Tool approval requires decision approve or reject"
                    )
                    return {}
                choices = [
                    fixed if fixed is not None else {"type": msg["decision"]} for fixed in policy
                ]
                result = {
                    "decisions": choices,
                    "any_rejected": any(d["type"] == "reject" for d in choices),
                }
            elif kind == "interrupt_reply" and isinstance(msg.get("result"), dict):
                result = msg["result"]
            else:
                self._reject(
                    "invalid_reply", "This interrupt requires an object-valued interrupt_reply"
                )
                return {}
            self._public_interrupts.pop(iid)
            return {"t": "interrupt_reply", "id": internal_id, "result": result}
        if kind in ("cancel", "shutdown"):
            self._cancel_queued()
            return {"t": kind}
        if kind == "ping":
            self.publish("pong", id=msg.get("id"), active_request=self._turn_id)
            return {}
        self._reject(
            "unknown_command",
            "Supported commands: prompt, approval, interrupt_reply, cancel, ping, shutdown",
        )
        return {}

    def _cancel_queued(self):
        for pid, *_ in self._queued:
            self._finish(pid, "cancelled")
        self._queued.clear()

    async def _forward_interrupt(self, event):
        try:
            await self._handle_interrupt(event)
        finally:
            self._public_interrupts.clear()
            if not event.future.done():
                event.future.set_result(default_interrupt_response(event.kind))

    async def _handle_interrupt(self, event):
        if event.kind == "tool":
            payload = event.payload if isinstance(event.payload, dict) else {}
            self._policy = await asyncio.to_thread(
                evaluate_tool_actions,
                payload,
                self.session_state,
                plan_mode_enabled=getattr(self.session_state, "plan_mode_enabled", False),
                user_request=self._current_prompt,
            )
            if getattr(self.session_state, "headless_deny_tools", False):
                self._policy = [
                    choice if choice is not None else {"type": "reject"} for choice in self._policy
                ]
            if all(choice is not None for choice in self._policy):
                if not event.future.done():
                    event.future.set_result(
                        {
                            "decisions": self._policy,
                            "any_rejected": any(
                                choice["type"] == "reject" for choice in self._policy
                            ),
                        }
                    )
                return
        else:
            self._policy = []
        try:
            await asyncio.wait_for(
                super()._forward_interrupt(event),
                timeout=getattr(self.session_state, "pipe_approval_timeout", 300),
            )
        except TimeoutError:
            self._status = "approval_timeout"
            raise RuntimeError("Human input timed out") from None
        finally:
            self._public_interrupts.clear()
            if not event.future.done():
                event.future.set_result(default_interrupt_response(event.kind))

    async def _run_turn(self, pid, text, images=None, auto_approve=None):
        self._terminal, self._status, self._completed = None, "running", False
        self._current_prompt = text
        self.publish("started", request_id=pid)
        try:
            async with asyncio.timeout(
                getattr(self.session_state, "pipe_request_timeout", None)
            ) as budget:
                await super()._run_turn(pid, text, images, auto_approve)
            if budget.expired():
                self._status = "timeout"
        except TimeoutError:
            self._status = "timeout"
        except asyncio.CancelledError:
            self._status = "cancelled"
            raise
        finally:
            status = self._status
            if status == "running":
                status = (
                    "success"
                    if self._completed and self._terminal and self._terminal["ok"]
                    else "error"
                )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._save(), timeout=5)
            self._finish(pid, status)

    async def _save(self):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(super()._save(), timeout=5)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        # Private worker consumes internal messages; conversion stays confined
        # to its inbox so the tab protocol remains unchanged.
        inbox_get = self.inbox.get

        async def public_get():
            return self.translate(await inbox_get())

        self.inbox.get = public_get
        code = await super().run()
        if self._output_closed:
            code = 141
        if not getattr(self.session_state, "_pipe_startup_timer", None):
            self.publish("stopped", exit_code=code)
            self.session_state.pipe_stopped = True
        return code


async def run_pipe_session(**kwargs):
    return await PipeSession(**kwargs).run()


async def run_pipe_bootstrap(awaitable, state):
    """Bound startup independently of the lifetime of the persistent session."""
    state.pipe_instance_id = uuid.uuid4().hex
    task = asyncio.current_task()
    expired = False

    def expire():
        nonlocal expired
        if not getattr(state, "pipe_ready", False):
            expired = True
            task.cancel()

    timer = asyncio.get_running_loop().call_later(state.pipe_startup_timeout, expire)
    state._pipe_startup_timer = timer
    code = 0
    message = None
    try:
        await awaitable
        code = state.headless_exit_code
        if not getattr(state, "pipe_ready", False) and code == 0:
            code = 1
            message = "Process ended before readiness"
    except asyncio.CancelledError:
        code = 124 if expired else 130
        message = "Startup timed out" if expired else "Process cancelled"
    except (Exception, SystemExit) as exc:
        code = 1
        message = f"{type(exc).__name__}: {exc}"
    finally:
        timer.cancel()
        state.headless_exit_code = code
    if not getattr(state, "pipe_stopped", False):
        output = HeadlessOutput("stream-json", state.session_id, None, fd=state.headless_out_fd)
        with contextlib.suppress(OSError, ValueError):
            emit = getattr(state, "_pipe_emit", None)
            if emit:
                if message:
                    emit(
                        "error",
                        code="startup_timeout" if expired else "process_error",
                        message=message,
                    )
                emit("stopped", exit_code=code)
                state.pipe_stopped = True
                return
            if message:
                output._writeln(
                    {
                        "type": "error",
                        "code": "startup_timeout" if expired else "process_error",
                        "message": message,
                    }
                )
            output._writeln({"type": "stopped", "exit_code": code})
