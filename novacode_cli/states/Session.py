import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from novacode_cli.states.slices import (
    AgentRuntimeState,
    BackgroundTaskState,
    NotificationState,
    RemoteBridgeState,
    UISettings,
    WikiState,
)


@dataclass
class Notification:
    """A single in-terminal notification event."""

    id: str  # short uuid hex
    level: str  # "info" | "success" | "warning" | "error" | "approval"
    title: str  # short headline (e.g. "Ralph task completed")
    message: str  # longer description
    source: str  # e.g. "ralph", "tests", "process", "system"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    dismissed: bool = False
    action_id: str | None = None  # Key into SessionState._pending_approvals
    action_type: str | None = None  # "approve" | "select" | "input"


class RalphTaskStatus(Enum):
    """Status of a Ralph background task."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BackgroundRalphTask:
    """Tracks a Ralph background task."""
    task_id: str  # Unique identifier
    iteration: int  # Current iteration number
    max_iterations: int  # Total iterations
    task_description: str  # What ralph is working on
    status: RalphTaskStatus = RalphTaskStatus.RUNNING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    asyncio_task: object = field(default=None, repr=False)  # Reference to asyncio.Task
    working_directory: str = "."
    error_message: str | None = None


class SessionState:
    """Holds mutable session state across all domains.

    SessionState is a composite container that holds 6 focused state slices:

        UISettings         — auto-approve, plan mode, verbose, hints, splash
        AgentRuntimeState  — agent graph, backend, model, plan agent
        RemoteBridgeState  — Discord/Telegram bridge queue, lock, manager
        BackgroundTaskState — Ralph tasks, Trello server
        NotificationState  — in-terminal notification deque
        WikiState          — wiki metadata (page count, last ingest)

    Properties on this class delegate to the appropriate slice, so **zero
    callers need to change**. Dynamically-assigned fields (e.g.
    ``browser_use_tasks``, ``current_task``) continue to work via
    __getattr__ / __setattr__.
    """

    def __init__(self, auto_approve: bool = False, no_splash: bool = False) -> None:
        # -- focused state slices ---------------------------------------------
        self._ui_settings = UISettings(auto_approve=auto_approve, no_splash=no_splash)
        self._agent_runtime = AgentRuntimeState()
        self._remote_bridge = RemoteBridgeState()
        self._bg_tasks = BackgroundTaskState()
        self._ntf = NotificationState()
        self._wiki = WikiState()

        # -- cross-cutting session identity fields ----------------------------
        self.thread_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.is_continued: bool = False
        self.todos: list[dict] | None = None

        # Steering instructions: persistent user guidance injected into
        # every model call.  List of SteeringInstruction(label, instruction).
        from novacode_cli.bootstrap.steering import SteeringInstruction
        self.steering_instructions: list[SteeringInstruction] = []

        # Dynamic fields (plugin/external only) stored here
        self._dynamic: dict[str, Any] = {}

        # -- declared fields formerly invented dynamically by other modules ----
        # (each comment names the module(s) that read/write the field)
        self.use_tui: bool = True  # main.py
        self.headless: bool = False  # main.py, headless/runner.py
        self.headless_prompt: str | None = None  # main.py, headless/runner.py
        self.headless_output_format: str = "text"  # main.py, headless/runner.py
        self.headless_max_turns: int | None = None  # main.py, headless/runner.py
        self.headless_deny_tools: bool = False  # main.py, headless/runner.py
        self.headless_out_fd: int | None = None  # main.py, headless/runner.py
        self.headless_exit_code: int = 0  # main.py, headless/runner.py
        self.workspace_root: str | None = None  # commands/log_commands.py, tui/app.py
        self.verify_enabled: bool = False  # ui/execution.py
        self.active_goal: str | None = None  # commands/side_commands.py, core/agent_loop.py, tui/app.py
        self.active_rubric: str | None = None  # commands/side_commands.py, core/agent_loop.py
        self.browser_use_tasks: dict = {}  # commands/browser_use_handler.py
        self.create_server: Any = None  # commands/create_handler.py, tui/app.py
        self._cron_scheduler: Any = None  # commands/cron_handler.py, main.py
        self._webhook_server: Any = None  # commands/webhook_handler.py
        self._ralph_stop_requested: bool = False  # commands/ralph_handler.py, tui/app.py
        self._ralph_checkpoint_requested: bool = False  # commands/ralph_handler.py, tui/app.py
        self._background_threads: list = []  # commands/ralph_handler.py
        self._voice_pipeline: Any = None  # main.py, tui/app.py
        self._remote_tool_notify: Any = None  # remote/processor.py, ui/execution.py
        self._remote_todo_notify: Any = None  # remote/processor.py, ui/execution.py
        self._remote_processor_task: Any = None  # main.py (shutdown)

        # Pending approval Futures keyed by action_id.
        # When the agent loop yields an InterruptRequest, the Future is stored
        # here alongside a notification so the user can approve/reject via
        # /notifications approve <id> or platform-specific buttons.
        self._pending_approvals: dict[str, asyncio.Future] = {}

    # ══════════════════════════════════════════════════════════════════════
    # Backward-compatible properties — delegate to UI settings slice
    # ══════════════════════════════════════════════════════════════════════

    @property
    def auto_approve(self) -> bool:
        return self._ui_settings.auto_approve

    @auto_approve.setter
    def auto_approve(self, value: bool) -> None:
        self._ui_settings.auto_approve = value

    @property
    def no_splash(self) -> bool:
        return self._ui_settings.no_splash

    @no_splash.setter
    def no_splash(self, value: bool) -> None:
        self._ui_settings.no_splash = value

    @property
    def plan_mode_enabled(self) -> bool:
        return self._ui_settings.plan_mode_enabled

    @plan_mode_enabled.setter
    def plan_mode_enabled(self, value: bool) -> None:
        self._ui_settings.plan_mode_enabled = value

    @property
    def verbose(self) -> bool:
        return self._ui_settings.verbose

    @verbose.setter
    def verbose(self, value: bool) -> None:
        self._ui_settings.verbose = value

    @property
    def exit_hint_until(self) -> float | None:
        return self._ui_settings.exit_hint_until

    @exit_hint_until.setter
    def exit_hint_until(self, value: float | None) -> None:
        self._ui_settings.exit_hint_until = value

    @property
    def exit_hint_handle(self) -> Any:
        return self._ui_settings.exit_hint_handle

    @exit_hint_handle.setter
    def exit_hint_handle(self, value: Any) -> None:
        self._ui_settings.exit_hint_handle = value

    # ══════════════════════════════════════════════════════════════════════
    # Backward-compatible properties — delegate to agent runtime slice
    # ══════════════════════════════════════════════════════════════════════

    @property
    def token_tracker(self) -> Any:
        return self._agent_runtime.token_tracker

    @token_tracker.setter
    def token_tracker(self, value: Any) -> None:
        self._agent_runtime.token_tracker = value

    @property
    def _agent(self) -> Any:
        return self._agent_runtime._agent  # type: ignore[attr-defined]

    @_agent.setter
    def _agent(self, value: Any) -> None:
        self._agent_runtime._agent = value  # type: ignore[attr-defined]

    @property
    def _backend(self) -> Any:
        return self._agent_runtime._backend  # type: ignore[attr-defined]

    @_backend.setter
    def _backend(self, value: Any) -> None:
        self._agent_runtime._backend = value  # type: ignore[attr-defined]

    @property
    def _checkpointer(self) -> Any:
        return self._agent_runtime._checkpointer  # type: ignore[attr-defined]

    @_checkpointer.setter
    def _checkpointer(self, value: Any) -> None:
        self._agent_runtime._checkpointer = value  # type: ignore[attr-defined]

    @property
    def _store(self) -> Any:
        return self._agent_runtime._store  # type: ignore[attr-defined]

    @_store.setter
    def _store(self, value: Any) -> None:
        self._agent_runtime._store = value  # type: ignore[attr-defined]

    @property
    def _tools(self) -> list:
        return self._agent_runtime._tools  # type: ignore[attr-defined]

    @_tools.setter
    def _tools(self, value: list) -> None:
        self._agent_runtime._tools = value  # type: ignore[attr-defined]

    @property
    def _assistant_id(self) -> str | None:
        return self._agent_runtime._assistant_id  # type: ignore[attr-defined]

    @_assistant_id.setter
    def _assistant_id(self, value: str | None) -> None:
        self._agent_runtime._assistant_id = value  # type: ignore[attr-defined]

    @property
    def _model(self) -> Any:
        return self._agent_runtime._model  # type: ignore[attr-defined]

    @_model.setter
    def _model(self, value: Any) -> None:
        self._agent_runtime._model = value  # type: ignore[attr-defined]

    @property
    def _sandbox_type(self) -> str | None:
        return self._agent_runtime._sandbox_type  # type: ignore[attr-defined]

    @_sandbox_type.setter
    def _sandbox_type(self, value: str | None) -> None:
        self._agent_runtime._sandbox_type = value  # type: ignore[attr-defined]

    @property
    def plan_agent(self) -> Any:
        return self._agent_runtime.plan_agent

    @plan_agent.setter
    def plan_agent(self, value: Any) -> None:
        self._agent_runtime.plan_agent = value

    @property
    def plan_backend(self) -> Any:
        return self._agent_runtime.plan_backend

    @plan_backend.setter
    def plan_backend(self, value: Any) -> None:
        self._agent_runtime.plan_backend = value

    @property
    def plan_content(self) -> str | None:
        return self._agent_runtime.plan_content

    @plan_content.setter
    def plan_content(self, value: str | None) -> None:
        self._agent_runtime.plan_content = value

    @property
    def approved_plan_content(self) -> str | None:
        return self._agent_runtime.approved_plan_content

    @approved_plan_content.setter
    def approved_plan_content(self, value: str | None) -> None:
        self._agent_runtime.approved_plan_content = value

    # ══════════════════════════════════════════════════════════════════════
    # Backward-compatible properties — delegate to remote bridge slice
    # ══════════════════════════════════════════════════════════════════════

    @property
    def _remote_message_queue(self) -> Any:
        return self._remote_bridge._remote_message_queue

    @_remote_message_queue.setter
    def _remote_message_queue(self, value: Any) -> None:
        self._remote_bridge._remote_message_queue = value

    @property
    def _remote_message_lock(self) -> Any:
        return self._remote_bridge._remote_message_lock

    @_remote_message_lock.setter
    def _remote_message_lock(self, value: Any) -> None:
        self._remote_bridge._remote_message_lock = value

    @property
    def _remote_bridge_manager(self) -> Any:
        return self._remote_bridge._remote_bridge_manager

    @_remote_bridge_manager.setter
    def _remote_bridge_manager(self, value: Any) -> None:
        self._remote_bridge._remote_bridge_manager = value

    @property
    def _pre_remote_auto_approve(self) -> bool | None:
        return self._remote_bridge._pre_remote_auto_approve

    @_pre_remote_auto_approve.setter
    def _pre_remote_auto_approve(self, value: bool | None) -> None:
        self._remote_bridge._pre_remote_auto_approve = value

    @property
    def _image_tracker(self) -> Any:
        return self._remote_bridge._image_tracker

    @_image_tracker.setter
    def _image_tracker(self, value: Any) -> None:
        self._remote_bridge._image_tracker = value

    @property
    def _seen_message_ids(self) -> set:
        return self._remote_bridge._seen_message_ids

    @_seen_message_ids.setter
    def _seen_message_ids(self, value: set) -> None:
        from novacode_cli.states.slices.remote_bridge import _BoundedSet

        # Wrap plain sets so the size bound survives reassignment (callers pass
        # ``set()`` at session init).
        if not isinstance(value, _BoundedSet):
            value = _BoundedSet(value)
        self._remote_bridge._seen_message_ids = value

    @property
    def _composite_backend(self) -> Any:
        return self._remote_bridge._composite_backend

    @_composite_backend.setter
    def _composite_backend(self, value: Any) -> None:
        self._remote_bridge._composite_backend = value

    @property
    def _console(self) -> Any:
        return self._remote_bridge._console

    @_console.setter
    def _console(self, value: Any) -> None:
        self._remote_bridge._console = value

    # ══════════════════════════════════════════════════════════════════════
    # Backward-compatible properties — delegate to background tasks slice
    # ══════════════════════════════════════════════════════════════════════

    @property
    def background_ralph_tasks(self) -> dict:
        return self._bg_tasks.background_ralph_tasks

    @background_ralph_tasks.setter
    def background_ralph_tasks(self, value: dict) -> None:
        self._bg_tasks.background_ralph_tasks = value

    @property
    def trello_server(self) -> Any:
        return self._bg_tasks.trello_server

    @trello_server.setter
    def trello_server(self, value: Any) -> None:
        self._bg_tasks.trello_server = value

    # ══════════════════════════════════════════════════════════════════════
    # Backward-compatible properties — delegate to notification slice
    # ══════════════════════════════════════════════════════════════════════

    @property
    def notifications(self) -> deque:
        return self._ntf.notifications

    @notifications.setter
    def notifications(self, value: deque) -> None:
        self._ntf.notifications = value

    # ══════════════════════════════════════════════════════════════════════
    # Backward-compatible properties — delegate to wiki slice
    # ══════════════════════════════════════════════════════════════════════

    @property
    def wiki_root(self) -> str | None:
        return self._wiki.wiki_root

    @wiki_root.setter
    def wiki_root(self, value: str | None) -> None:
        self._wiki.wiki_root = value

    @property
    def wiki_enabled(self) -> bool:
        return self._wiki.wiki_enabled

    @wiki_enabled.setter
    def wiki_enabled(self, value: bool) -> None:
        self._wiki.wiki_enabled = value

    @property
    def wiki_page_count(self) -> int:
        return self._wiki.page_count

    @wiki_page_count.setter
    def wiki_page_count(self, value: int) -> None:
        self._wiki.page_count = value

    @property
    def wiki_last_ingest(self) -> str | None:
        return self._wiki.last_ingest

    @wiki_last_ingest.setter
    def wiki_last_ingest(self, value: str | None) -> None:
        self._wiki.last_ingest = value

    # ══════════════════════════════════════════════════════════════════════
    # Dynamic-field fallback
    # ══════════════════════════════════════════════════════════════════════
    # Every field used by Nova's own code is declared in __init__ (and listed
    # in _CONCRETE_FIELDS below); _dynamic only serves genuinely external /
    # plugin-assigned fields.

    # Names set in __init__ as plain instance attributes. __setattr__ must
    # route these through object.__setattr__ so they stay concrete instead of
    # falling into _dynamic.
    _CONCRETE_FIELDS = frozenset({
        "thread_id", "session_id", "is_continued", "todos",
        "steering_instructions",
        "_ui_settings", "_agent_runtime", "_remote_bridge", "_bg_tasks", "_ntf",
        "_wiki", "_dynamic", "_pending_approvals",
        # formerly-dynamic fields declared in __init__
        "use_tui", "headless", "headless_prompt", "headless_output_format",
        "headless_max_turns", "headless_deny_tools", "headless_out_fd",
        "headless_exit_code",
        "workspace_root", "verify_enabled", "active_goal", "active_rubric",
        "browser_use_tasks", "create_server",
        "_cron_scheduler", "_webhook_server",
        "_ralph_stop_requested", "_ralph_checkpoint_requested",
        "_background_threads", "_voice_pipeline",
        "_remote_tool_notify", "_remote_todo_notify", "_remote_processor_task",
    })

    def __getattr__(self, name: str) -> Any:
        # First, check if the name is a private agent field on the runtime slice
        agent_private = {"_backend", "_checkpointer", "_store", "_tools",
                         "_assistant_id", "_model", "_sandbox_type"}
        if name in agent_private:
            return getattr(self._agent_runtime, name, None)
        # Then check dynamic fields
        try:
            return object.__getattribute__(self, "_dynamic")[name]
        except KeyError:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )

    def __setattr__(self, name: str, value: Any) -> None:
        # Bypass for known instance attributes (set normally)
        if name in self._CONCRETE_FIELDS:
            object.__setattr__(self, name, value)
            return
        # Private agent fields go to the agent runtime slice
        agent_private = {"_backend", "_checkpointer", "_store", "_tools",
                         "_assistant_id", "_model", "_sandbox_type",
                         "_agent"}  # _agent (compiled graph) goes to runtime slice
        if name in agent_private:
            setattr(self._agent_runtime, name, value)
            return
        # Property-backed fields — use object.__setattr__ which triggers the
        # property descriptor (data descriptors always win).
        if name in {
            "auto_approve", "no_splash", "plan_mode_enabled", "verbose",
            "exit_hint_until", "exit_hint_handle",
            "token_tracker", "plan_agent", "plan_backend", "plan_content",
            "approved_plan_content",
            "_remote_message_queue", "_remote_message_lock",
            "_remote_bridge_manager", "_pre_remote_auto_approve",
            "_image_tracker", "_seen_message_ids", "_composite_backend",
            "_console",
            "background_ralph_tasks", "trello_server",
            "notifications",
            "wiki_root", "wiki_enabled", "wiki_page_count", "wiki_last_ingest",
        }:
            object.__setattr__(self, name, value)
            return
        # Everything else -> dynamic storage
        try:
            dyn = object.__getattribute__(self, "_dynamic")
        except AttributeError:
            dyn = {}
            object.__setattr__(self, "_dynamic", dyn)
        dyn[name] = value

    # ══════════════════════════════════════════════════════════════════════
    # Delegating methods
    # ══════════════════════════════════════════════════════════════════════

    def set_agent_context(
        self,
        agent: Any,
        backend: Any,
        checkpointer: Any,
        store: Any,
        tools: list,
        assistant_id: str,
        model: Any,
        sandbox_type: str | None = None,
        sandbox_id: str | None = None,
        sandbox: Any = None,
    ) -> None:
        """Set the agent context for dynamic model switching."""
        self._agent_runtime.set_agent_context(
            agent, backend, checkpointer, store, tools,
            assistant_id, model, sandbox_type, sandbox_id, sandbox,
        )

    async def switch_model(self, new_model: Any) -> tuple[Any, Any]:
        """Switch to a new model dynamically.

        Delegates to AgentRuntimeState.switch_model() and passes
        steering_instructions and session_id for hook dispatch.
        """
        return await self._agent_runtime.switch_model(
            new_model,
            steering_instructions=self.steering_instructions,
            session_id=self.session_id,
        )

    async def reload_mcp_servers(self) -> tuple[Any, Any]:
        """Reload MCP servers dynamically.

        Delegates to AgentRuntimeState.reload_mcp_servers() and passes
        steering_instructions.
        """
        return await self._agent_runtime.reload_mcp_servers(
            steering_instructions=self.steering_instructions,
        )

    def toggle_auto_approve(self) -> bool:
        """Toggle auto-approve and return new state."""
        return self._ui_settings.toggle_auto_approve()

    def toggle_plan_mode(self) -> bool:
        """Toggle plan mode and return new state."""
        return self._ui_settings.toggle_plan_mode()

    def toggle_verbose(self) -> bool:
        """Toggle verbose mode and return new state."""
        return self._ui_settings.toggle_verbose()

    def get_active_agent(self) -> tuple[Any, Any]:
        """Get the active agent based on plan mode."""
        return self._agent_runtime.get_active_agent(
            plan_mode_enabled=self._ui_settings.plan_mode_enabled,
        )

    def clear_plan_agent(self) -> None:
        """Clear the plan agent after plan approval."""
        self._agent_runtime.clear_plan_agent()
        self._ui_settings.plan_mode_enabled = False

    def reset_conversation(self) -> None:
        """Total reset for ``/clear`` — begin a brand-new conversation with no
        carried-over context.

        Assigns fresh ``thread_id``/``session_id`` so the checkpointer returns
        an empty message history, and clears every piece of per-conversation
        runtime context so nothing leaks into the new chat:

        - ``todos`` and continuation flag
        - persistent ``steering_instructions`` (from ``/steer``)
        - plan mode: exits plan mode and drops the cached plan agent, the
          in-flight plan, and any approved-but-unconsumed plan

        Preserved by design (these are not conversation context): long-term
        memory files (``agent.md`` / ``USER.md`` / ``MEMORY.md`` / ``NOVA.md``),
        the Nova learning store, the compiled agent + backend + model, and
        explicit UX toggles (``auto_approve``, ``verbose``, ``no_splash``).
        """
        self.thread_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.is_continued = False
        self.todos = None
        # Clear in place — the agent's SteeringMiddleware holds a reference to
        # THIS list, so reassigning (= []) would orphan it and silently break
        # steering after /clear. Mutate the shared list instead.
        if isinstance(self.steering_instructions, list):
            self.steering_instructions.clear()
        else:
            self.steering_instructions = []
        # Plan mode: exit, drop cached plan agent + in-flight plan content, and
        # discard any approved-but-unconsumed plan.
        self.clear_plan_agent()
        self.consume_approved_plan()
        # Agent-to-agent mailbox is per-conversation; start the new chat empty.
        try:
            from novacode_cli.agents.agent_mailbox import clear_mailbox

            clear_mailbox()
        except Exception:  # noqa: BLE001
            pass

    def set_plan_content(self, plan_content: str) -> None:
        """Store current plan content from plan agent."""
        self._agent_runtime.set_plan_content(plan_content)

    def set_approved_plan(self, plan_content: str) -> None:
        """Store approved plan content for Nova agent execution."""
        self._agent_runtime.set_approved_plan(plan_content)

    def consume_approved_plan(self) -> str | None:
        """Get and clear the approved plan content."""
        return self._agent_runtime.consume_approved_plan()

    # -- notifications delegated with hook dispatch -----------------------

    def add_notification(
        self,
        level: str,
        title: str,
        message: str,
        source: str,
        *,
        action_id: str | None = None,
        action_type: str | None = None,
    ) -> str:
        """Create and store a notification, fire the notification hook, return id.

        When *action_id* is set the notification represents a *pending approval*
        — see :meth:`register_pending_approval` and :meth:`resolve_approval`.
        """
        nid = self._ntf.add(level, title, message, source, action_id=action_id, action_type=action_type)
        # Fire hook with session_id (cross-domain coordination in SessionState)
        try:
            from novacode_cli.hooks import HookEvent, dispatch_hook_fire_and_forget

            n = self._ntf.notifications[0]
            dispatch_hook_fire_and_forget(
                HookEvent.NOTIFICATION,
                {
                    "id": n.id,
                    "level": n.level,
                    "title": n.title,
                    "message": n.message,
                    "source": n.source,
                    "timestamp": n.timestamp.isoformat(),
                    "session_id": self.session_id,
                    "action_id": n.action_id,
                    "action_type": n.action_type,
                },
            )
        except Exception:  # noqa: BLE001 — notifications must never break callers
            pass
        return nid

    def dismiss_notification(self, notification_id: str) -> bool:
        """Mark a notification as dismissed. Returns True if found."""
        return self._ntf.dismiss(notification_id)

    def clear_notifications(self) -> int:
        """Drop all notifications. Returns how many were removed."""
        return self._ntf.clear()

    def unread_notification_count(self) -> int:
        """Number of non-dismissed notifications."""
        return self._ntf.unread_count()

    # -- pending approval resolution ---------------------------------------

    def register_pending_approval(
        self, action_id: str, future: asyncio.Future
    ) -> None:
        """Store a Future that the approval flow must resolve.

        Called by the agent loop when an ``InterruptRequest`` is yielded.
        The caller (TUI, CLI, or remote bridge) resolves the Future via
        :meth:`resolve_approval` or :meth:`dismiss_notification`.
        """
        self._pending_approvals[action_id] = future

    def resolve_approval(self, action_id: str, approve: bool = True) -> bool:
        """Resolve a pending approval Future.

        Args:
            action_id: The key (matches ``Notification.action_id``).
            approve: ``True`` to approve, ``False`` to reject.

        Returns:
            ``True`` if the action was found and resolved, ``False`` if no
            pending approval with that key exists.
        """
        fut = self._pending_approvals.pop(action_id, None)
        if fut is None or fut.done():
            return False
        if approve:
            fut.set_result(
                {"decisions": [{"type": "approve"}], "any_rejected": False}
            )
        else:
            fut.set_result(
                {
                    "decisions": [{"type": "reject", "message": "Rejected by user"}],
                    "any_rejected": True,
                }
            )
        # Also dismiss the corresponding notification.
        for n in self._ntf.notifications:
            if n.action_id == action_id and not n.dismissed:
                n.dismissed = True
                break
        return True

    def pending_approval_count(self) -> int:
        """Number of unresolved approval notifications.

        Returns 0 immediately when auto-approve is enabled (remote sessions
        auto-approve everything — no human is being asked, so a flashing badge
        is just noise).
        """
        if self.auto_approve:
            return 0
        return sum(
            1
            for n in self._ntf.notifications
            if not n.dismissed and n.action_type == "approve"
        )
