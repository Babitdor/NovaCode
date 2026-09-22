"""FastAPI application for the Nova agent.

Exposes the agent runtime through:
- ``GET /health`` — health check
- ``POST /sessions`` — create a new agent session
- ``DELETE /sessions/{session_id}`` — delete a session
- ``WebSocket /ws/{session_id}`` — streaming agent communication

All agent output is emitted as structured JSON events (see
:mod:`novacode_cli.server.event_adapter`). No terminal rendering is performed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from novacode_cli import ui_events
from novacode_cli.core.agent_loop import default_interrupt_response, iterate_agent_events
from novacode_cli.server.event_adapter import serialize_event
from novacode_cli.server.session_manager import SessionManager

logger = logging.getLogger(__name__)

# ── Cowork IPC auth ───────────────────────────────────────────────────
# The server binds localhost only, but any local process (or a browser page via
# a same-machine fetch) could otherwise reach the workspace-grant/session routes
# and bypass the whole WorkspacePolicy boundary. A per-process token, handed to
# the /cowork page at launch, gates every sensitive route. Accepted from the
# ``X-Cowork-Token`` header (fetch) or a ``token`` query param (the initial page
# load and the WebSocket, which cannot set headers).
# ponytail: token in the /cowork URL can leak via browser history; acceptable for
# a single-user localhost tool. Move to a POST'd cookie if that ever matters.
_COWORK_TOKEN = secrets.token_urlsafe(32)


def get_cowork_token() -> str:
    """The per-process Cowork IPC token (the launcher embeds it in the URL)."""
    return _COWORK_TOKEN


def _token_ok(supplied: str | None) -> bool:
    return bool(supplied) and secrets.compare_digest(supplied, _COWORK_TOKEN)


def _require_token(
    x_cowork_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    """FastAPI dependency: 401 unless a valid token is present (header or query)."""
    if not _token_ok(x_cowork_token or token):
        raise HTTPException(status_code=401, detail="missing or invalid cowork token")


# ── Session manager (module-level singleton) ──────────────────────────
session_manager = SessionManager()


# ── Agent factory ──────────────────────────────────────────────────────
def _create_server_agent(
    workspace_root: str | None = None,
    extra_middleware: list | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    """Create an agent instance for the server.

    Mirrors the CLI's agent creation (``main.py``) but without CLI-specific
    setup like BootAnimation, voice preloading, or session state.

    Does NOT import ``novacode_cli.main`` to avoid triggering Serena MCP
    startup and other CLI-specific side effects.
    """
    from novacode_cli.config.model_create import create_model

    model = create_model()

    # ── Tools (mirrors the CLI tool list from main.py) ──────────────
    from novacode_cli.tools import (
        docs_search,
        duckduckgo_search,
        fetch_url,
        forget,
        github_trending,
        hacker_news,
        linkedin_jobs,
        list_memories,
        oracle,
        package_info,
        read_memory,
        recall,
        reddit_posts,
        remember,
        skill_manage,
        speak,
        think,
        wiki_read,
        wiki_search,
        wiki_update_index,
        wiki_write,
        write_memory,
    )
    from novacode_cli.tools.plan_mode_tools import (
        ask_user_question,
        enter_plan_mode,
        exit_plan_mode,
    )

    tools = [
        fetch_url,
        ask_user_question,
        enter_plan_mode,
        exit_plan_mode,
        wiki_read,
        wiki_search,
        wiki_update_index,
        wiki_write,
        package_info,
        think,
        speak,
        oracle,
        skill_manage,
        duckduckgo_search,
        docs_search,
        github_trending,
        hacker_news,
        linkedin_jobs,
        reddit_posts,
        write_memory,
        read_memory,
        remember,
        recall,
        list_memories,
        forget,
    ]

    # Conditionally add Semble-powered code search tools
    from novacode_cli.tools import code_search, find_related_code

    if code_search is not None:
        tools.append(code_search)
    if find_related_code is not None:
        tools.append(find_related_code)

    # Conditionally add Tavily web search
    from novacode_cli.config.config import settings

    if settings.has_tavily:
        from novacode_cli.tools import web_search

        tools.append(web_search)

    # File recovery tools
    from novacode_cli.recovery import get_recovery_manager, list_trash, restore_file

    get_recovery_manager(
        session_id="server",
        workspace_root=Path.cwd(),
    )
    tools.extend([list_trash, restore_file])

    # Artifact tools (parity with the TUI agent).
    from novacode_cli.tools.artifact_tools import (
        create_artifact,
        list_artifacts,
        update_artifact,
    )

    tools.extend([create_artifact, update_artifact, list_artifacts])

    # ── Create agent ─────────────────────────────────────────────────
    from novacode_cli.agents.core_agent import create_agent_with_config

    assistant_id = "nova-server"
    store = InMemoryStore()
    checkpointer = InMemorySaver()

    agent, composite_backend = create_agent_with_config(
        model,
        assistant_id,
        tools,
        sandbox=None,
        sandbox_type=None,
        store=store,
        checkpointer=checkpointer,
        steering_instructions=None,
        exec_sandbox=False,
        session_id="server",
        workspace_root=workspace_root,
        extra_middleware=extra_middleware,
        # Cowork's security boundary is the WorkspacePolicy broker (default-deny,
        # authorizes every file/shell op against the granted folder), NOT per-tool
        # HITL approval. The browser SPA has no approve/deny UI, so leaving HITL on
        # makes every write/execute turn emit an interrupt the client can't answer
        # → the run blocks ~300s and "hangs". Auto-approve here and let the broker
        # enforce; a denied op still returns an error ToolMessage, never executes.
        auto_approve=True,
    )

    config = {"configurable": {"thread_id": "server"}}
    return agent, composite_backend, config


# Cowork agents are built lazily, one per granted workspace root, each carrying
# the WorkspacePolicy broker middleware so the running agent is confined.
_cowork_agents: dict[str, tuple[Any, Any, dict[str, Any]]] = {}


def _get_cowork_agent() -> tuple[Any, Any, dict[str, Any]] | None:
    """Build (or reuse) the cowork agent rooted at the first active grant, with
    the broker middleware. Returns None if no workspace has been granted."""
    from novacode_cli.cowork.policy import get_policy

    grants = get_policy().active_grants()
    if not grants:
        return None
    root = grants[0].canonical
    cached = _cowork_agents.get(root)
    if cached is not None:
        return cached
    from novacode_cli.cowork.broker_middleware import CoworkBrokerMiddleware

    built = _create_server_agent(
        workspace_root=root, extra_middleware=[CoworkBrokerMiddleware(root)]
    )
    _cowork_agents[root] = built
    return built


# ── Pre-created agent (set by server_main before uvicorn starts) ──
_server_agent: Any = None
_server_backend: Any = None
_server_config: dict[str, Any] = {}


def set_agent(
    agent: Any,
    backend: Any,
    config: dict[str, Any],
) -> None:
    """Set the pre-created agent (called from server_main before startup)."""
    global _server_agent, _server_backend, _server_config
    _server_agent = agent
    _server_backend = backend
    _server_config = config


# ── Lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle."""
    logger.info("Nova server starting")
    yield
    # Shutdown: clean up all sessions
    for sid in list(session_manager._sessions):
        session_manager.delete_session(sid)
    logger.info("Nova server stopped")


app = FastAPI(
    title="Nova Agent API",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "sessions_active": session_manager.active_count,
    }


# ── Cowork desktop UI + workspace permission broker ────────────────────
@app.get("/cowork", dependencies=[Depends(_require_token)])
async def cowork_ui():
    """Serve the Nova Cowork single-page desktop UI (token-gated; the token is
    embedded so the page's fetch/WebSocket calls can authenticate)."""
    from fastapi.responses import HTMLResponse

    from novacode_cli.cowork.ui import render_cowork_html

    return HTMLResponse(render_cowork_html(_COWORK_TOKEN))


@app.get("/api/workspace", dependencies=[Depends(_require_token)])
async def cowork_workspace_list() -> dict[str, Any]:
    """List active (non-revoked) workspace grants."""
    from novacode_cli.cowork.policy import get_policy

    return {"grants": [g.public() for g in get_policy().active_grants()]}


@app.post("/api/workspace/grant", dependencies=[Depends(_require_token)])
async def cowork_workspace_grant(body: dict) -> Any:
    """Grant access to a folder. Default-deny stays in force for everything else."""
    from novacode_cli.cowork.policy import get_policy

    path = (body or {}).get("path", "")
    g = get_policy().grant(
        path,
        read=bool((body or {}).get("read", True)),
        write=bool((body or {}).get("write", True)),
        execute=bool((body or {}).get("execute", True)),
        recursive=bool((body or {}).get("recursive", True)),
    )
    if g is None:
        return JSONResponse(status_code=400, content={"error": "path not found or not a directory"})
    return {"grant": g.public()}


@app.post("/api/workspace/revoke", dependencies=[Depends(_require_token)])
async def cowork_workspace_revoke(body: dict) -> dict[str, Any]:
    """Revoke a grant — subsequent operations against it are denied immediately."""
    from novacode_cli.cowork.policy import get_policy

    return {"revoked": get_policy().revoke((body or {}).get("id", ""))}


@app.get("/api/authorize", dependencies=[Depends(_require_token)])
async def cowork_authorize(path: str, op: str = "read") -> dict[str, Any]:
    """Broker decision for a path+op — the authoritative boundary the agent's
    tools must consult (exposed here for the UI and for security tests)."""
    from novacode_cli.cowork.policy import get_policy

    d = get_policy().authorize(path, op)
    return {"allowed": d.allowed, "code": d.code, "reason": d.reason, "grant_id": d.grant_id}


# ── Session management ────────────────────────────────────────────────
@app.post("/sessions", dependencies=[Depends(_require_token)])
async def create_session() -> JSONResponse:
    """Create a new agent session.

    Returns the session ID. The caller should connect to
    ``/ws/{session_id}`` to interact with the agent.

    The agent is pre-created at startup and shared across sessions.
    Each session gets its own config with a unique thread_id.
    """
    # Cowork is default-deny: no agent/session exists until the user grants a
    # workspace. The agent is rooted at that grant and carries the WorkspacePolicy
    # broker middleware, so it is confined even if the model tries to escape.
    built = _get_cowork_agent()
    if built is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Grant a workspace folder first — Cowork denies all access until you do."},
        )
    agent, _backend, base_config = built
    config = {**base_config, "configurable": {"thread_id": uuid.uuid4().hex[:12]}}
    info = session_manager.create_session(agent, config)
    logger.info("Created cowork session %s", info.session_id)
    return JSONResponse(
        status_code=201,
        content={"session_id": info.session_id, "created_at": info.created_at},
    )


@app.delete("/sessions/{session_id:str}", dependencies=[Depends(_require_token)])
async def delete_session(session_id: str) -> JSONResponse:
    """Delete an agent session and cancel any in-progress run."""
    found = session_manager.delete_session(session_id)
    if not found:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session {session_id} not found"},
        )
    logger.info("Deleted session %s", session_id)
    return JSONResponse(
        status_code=200,
        content={"status": "deleted", "session_id": session_id},
    )


# ── WebSocket streaming ───────────────────────────────────────────────
@app.websocket("/ws/{session_id:str}")
async def websocket_handler(
    websocket: WebSocket, session_id: str, token: str | None = Query(default=None)
) -> None:
    """WebSocket endpoint for streaming agent communication.

    **Client → Server messages:**
    - ``{"type": "message", "data": {"content": "..."}}`` — send user input
    - ``{"type": "cancel"}`` — cancel the current run
    - ``{"type": "interrupt_response", "data": {...}}`` — respond to an interrupt

    **Server → Client events:**
    - ``assistant_token``, ``assistant_done``, ``tool_started``,
      ``tool_finished``, ``file_changed``, ``status``, ``error``,
      ``cancelled``, ``interrupt``, ``subagent_activity``, etc.
    """
    if not _token_ok(token):
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()

    info = session_manager.get_session(session_id)
    if info is None:
        await websocket.send_json({"type": "error", "data": {"message": f"Session {session_id} not found"}})
        await websocket.close()
        return

    cancel_event = asyncio.Event()
    info.set_cancel_event(cancel_event)

    try:
        await _handle_websocket(websocket, info, cancel_event)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for session %s", session_id)
    except Exception:
        logger.exception("WebSocket error for session %s", session_id)
    finally:
        info.clear_cancel_event()


class _ServerSessionState:
    """Minimal proxy session state — only the fields ``iterate_agent_events``
    reads. The cowork agent is built with ``auto_approve=True`` (the
    WorkspacePolicy broker is the boundary), so no tool interrupt is raised;
    plan interrupts are auto-approved, and ask_user_question interrupts are
    surfaced to the SPA's question dialog (see ``_run_agent_and_stream``).
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.auto_approve = True
        self.plan_mode_enabled = False
        self.plan_agent: Any = None
        self.plan_content: Any = None
        self.active_goal: str | None = None

    def add_notification(self, **_kw: Any) -> None:
        return None

    def dismiss_notification(self, _nid: Any) -> None:
        return None

    def register_pending_approval(self, _iid: Any, _fut: Any) -> None:
        return None

    def set_approved_plan(self, _plan: Any) -> None:
        return None

    def clear_plan_agent(self) -> None:
        return None


class _PendingQuestion:
    """Holds the asyncio.Future for an in-flight ``ask_user_question`` interrupt.

    The agent run (a background task) stores the future here when it surfaces a
    question; the WebSocket receive loop resolves it when the SPA posts an
    ``interrupt_response``. One at a time — the run is sequential.
    """

    def __init__(self) -> None:
        self.future: asyncio.Future | None = None


async def _handle_websocket(
    websocket: WebSocket,
    info: Any,
    cancel_event: asyncio.Event,
) -> None:
    """Main WebSocket message loop."""
    # One proxy session state per connection so the thread_id (hence the agent's
    # conversation memory) is stable across messages in this session.
    thread_id = info.config.get("configurable", {}).get("thread_id") or uuid.uuid4().hex[:12]
    session_state = _ServerSessionState(thread_id)
    seen_message_ids: set[str] = set()
    pending_question = _PendingQuestion()
    run_task: asyncio.Task | None = None

    try:
        while True:
            raw = await websocket.receive_json()

            msg_type = raw.get("type")
            data = raw.get("data", {})

            if msg_type == "cancel":
                cancel_event.set()
                if run_task is not None and not run_task.done():
                    run_task.cancel()
                continue

            if msg_type == "interrupt_response":
                # The SPA answered an ask_user_question interrupt — resume the
                # agent with the answer instead of the empty default.
                if pending_question.future is not None and not pending_question.future.done():
                    payload = data if isinstance(data, dict) else {}
                    # Accept both the TUI shape {"response": {...}} and a bare
                    # {"answer": ...}; the tool reads the answer at top level.
                    resp = payload.get("response", payload)
                    pending_question.future.set_result(resp)
                    pending_question.future = None
                else:
                    await websocket.send_json({
                        "type": "error",
                        "data": {"message": "No pending question interrupt"},
                    })
                continue

            if msg_type == "message":
                content = data.get("content", "")
                if not content:
                    await websocket.send_json({
                        "type": "error",
                        "data": {"message": "Empty message content"},
                    })
                    continue
                if run_task is not None and not run_task.done():
                    await websocket.send_json({
                        "type": "error",
                        "data": {"message": "Agent is still running — wait for it to finish."},
                    })
                    continue

                # Run the agent in the background so the receive loop stays live
                # and can answer question interrupts while the run is paused.
                run_task = asyncio.create_task(
                    _run_agent_and_stream(
                        websocket, info, content, session_state, seen_message_ids, pending_question,
                    )
                )
                continue

            # Unknown message type
            await websocket.send_json({
                "type": "error",
                "data": {"message": f"Unknown message type: {msg_type}"},
            })
    finally:
        # The client is gone (or the loop exited): never leave a question
        # interrupt hanging — resolve it to the benign default and stop the run.
        if pending_question.future is not None and not pending_question.future.done():
            pending_question.future.set_result(default_interrupt_response("question"))
        if run_task is not None and not run_task.done():
            run_task.cancel()


async def _run_agent_and_stream(
    websocket: WebSocket,
    info: Any,
    user_input: str,
    session_state: _ServerSessionState,
    seen_message_ids: set[str],
    pending_question: _PendingQuestion,
) -> None:
    """Run the agent on user input and stream events over the WebSocket.

    Mirrors the headless runner: drives the canonical ``iterate_agent_events``
    generator. ``ask_user_question`` interrupts are surfaced to the SPA (which
    has a question dialog) and the run waits for the user's answer; every other
    interrupt kind is auto-resolved so a run can never hang waiting on the
    (answer-less) browser client.
    """
    source = iterate_agent_events(
        user_input,
        info.agent,
        "nova-server",
        session_state,
        backend=getattr(info, "backend", None),
        seen_message_ids=seen_message_ids,
    )
    try:
        async for event in source:
            if isinstance(event, ui_events.InterruptRequest):
                if event.kind == "question":
                    # Send the interrupt to the client so the SPA can show the
                    # question dialog, then wait for the user's answer (resolved
                    # by an interrupt_response message from the receive loop).
                    serialized = serialize_event(event)
                    if serialized is not None:
                        await websocket.send_text(json.dumps(serialized, default=str))
                    pending_question.future = event.future
                    try:
                        await event.future
                    finally:
                        pending_question.future = None
                        if not event.future.done():
                            event.future.set_result(default_interrupt_response(event.kind))
                    continue
                # tool/plan interrupts: the SPA has no approve/deny UI — resolve
                # to the benign default so the run proceeds instead of blocking.
                # (Tool approvals don't reach here: auto_approve=True clears them.)
                if not event.future.done():
                    event.future.set_result(default_interrupt_response(event.kind))
                continue

            serialized = serialize_event(event)
            if serialized is not None:
                # Event data can contain non-JSON-native values (e.g. a
                # WindowsPath in a file/tool field). send_json() would raise
                # "Object of type WindowsPath is not JSON serializable" and abort
                # the whole run; default=str coerces any such value for display.
                await websocket.send_text(json.dumps(serialized, default=str))
                if isinstance(event, (ui_events.Cancelled, ui_events.Done)):
                    return
    except asyncio.CancelledError:
        try:
            await websocket.send_json({"type": "cancelled", "data": {}})
        except Exception:  # noqa: BLE001 — socket may already be closed
            pass
    except Exception as exc:
        logger.exception("Agent run failed")
        # Surface the real exception, not a generic string — a swallowed error
        # here is undebuggable from the browser (this hid a cross-agent tracker
        # clobber under concurrent TUI+Cowork use).
        await websocket.send_json({
            "type": "error",
            "data": {"message": f"Agent run failed: {type(exc).__name__}: {exc}"},
        })
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await source.aclose()
