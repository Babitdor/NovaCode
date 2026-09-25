"""Agent runtime state slice.

Owns the live agent graph, backend, checkpointer, store, tools, model,
sandbox type, token tracker, and plan-agent components. Fields here are
the machinery that makes the agent run — they have nothing to do with
UI configuration or remote bridges.
"""

from __future__ import annotations

import asyncio
from typing import Any


class AgentRuntimeState:
    """Agent infrastructure: graph, backend, checkpointer, model, plan agent."""

    def __init__(self) -> None:
        self._agent: Any = None          # The compiled agent graph
        self._backend: Any = None        # Composite backend
        self._checkpointer: Any = None   # Checkpointer for state preservation
        self._store: Any = None          # Store for memory
        self._tools: list = []           # List of tools
        self._assistant_id: str | None = None  # Agent ID
        self._model: Any = None          # Current model instance
        self._sandbox_type: str | None = None  # Sandbox type
        self._sandbox_id: str | None = None   # Sandbox/container ID
        # The REAL sandbox backend (None in local mode). Never the composite:
        # a rebuild that passed the composite as `sandbox` wrapped the local
        # filesystem as a `/workspace` sandbox after every /model switch or MCP
        # reload — reads failed with "unexpected server response: <no output>"
        # and writes landed in a stray <project>/workspace/ folder.
        self._sandbox: Any = None
        self.token_tracker: Any = None   # TokenTracker for toolbar context display
        self.plan_agent: Any = None      # Plan agent for planning phase
        self.plan_backend: Any = None    # Plan backend
        self.plan_content: str | None = None      # Current plan content
        self.approved_plan_content: str | None = None  # Approved plan content

        # Nova learning system state
        self.nova_tool_call_count: int = 0
        """In-memory tool call counter for Nova learning middleware
        (durable store is the source of truth, this is a fast in-memory cache)."""
        self.nova_last_review_time: float | None = None
        """Timestamp of the last Nova review cycle (None if never reviewed)."""

    # -- context management ---------------------------------------------------

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
        self._agent = agent
        self._backend = backend
        self._checkpointer = checkpointer
        self._store = store
        self._tools = tools
        self._assistant_id = assistant_id
        self._model = model
        self._sandbox_type = sandbox_type
        self._sandbox_id = sandbox_id
        self._sandbox = sandbox

    async def switch_model(
        self,
        new_model: Any,
        steering_instructions: list | None = None,
        session_id: str | None = None,
    ) -> tuple[Any, Any]:
        """Switch to a new model dynamically by recreating the agent.

        Args:
            new_model: The new model instance to use
            steering_instructions: Persistent user guidance (from SessionState)
            session_id: Session identifier for hook dispatch

        Returns:
            Tuple of (new_agent, new_backend)

        Raises:
            RuntimeError: If agent context is not set
        """
        if not all([self._agent, self._checkpointer, self._store, self._assistant_id]):
            raise RuntimeError("Agent context not set. Cannot switch model.")

        from novacode_cli.agents.core_agent import create_agent_with_config

        # Recreate agent with new model, preserving state.
        #
        # Off the event loop: this rebuild recompiles every subagent graph and
        # re-scans plugin skills/agents/MCP, which is seconds of synchronous work
        # that otherwise freezes the whole TUI (the stall watchdog recorded
        # multi-second freezes with the loop blocked in
        # create_agent_with_config -> _build_subagent_roster -> plugin_agent_specs
        # on /model switches). create_agent_with_config is sync and only builds
        # objects, so running it in a worker thread is safe.
        new_agent, new_backend = await asyncio.to_thread(
            create_agent_with_config,
            model=new_model,
            assistant_id=self._assistant_id,
            tools=self._tools,
            sandbox=self._sandbox,
            sandbox_type=self._sandbox_type,
            # Same rule as the first build in main.py: Pattern A confines the
            # local shell. Omitting it dropped confinement on every rebuild.
            exec_sandbox=self._sandbox is None and self._sandbox_type == "os",
            store=self._store,
            checkpointer=self._checkpointer,
            steering_instructions=steering_instructions or [],
            session_id=session_id or getattr(self, "session_id", None),
        )

        # Update stored references
        self._agent = new_agent
        self._backend = new_backend
        self._model = new_model

        # Fire model.switch hook
        try:
            from novacode_cli.hooks import dispatch_hook_fire_and_forget, HookEvent

            model_name = (
                getattr(new_model, "model_name", None)
                or getattr(new_model, "model", "unknown")
            )
            dispatch_hook_fire_and_forget(
                HookEvent.MODEL_SWITCH,
                {
                    "new_model": str(model_name),
                    "session_id": session_id or "",
                },
            )
        except Exception:  # noqa: BLE001 — notifications must never break callers
            pass

        return new_agent, new_backend

    async def reload_mcp_servers(
        self,
        steering_instructions: list | None = None,
    ) -> tuple[Any, Any]:
        """Reload MCP servers dynamically by resetting the middleware and recreating the agent.

        Args:
            steering_instructions: Persistent user guidance (from SessionState)

        Returns:
            Tuple of (new_agent, new_backend)

        Raises:
            RuntimeError: If agent context is not set
        """
        if not all([self._agent, self._checkpointer, self._store, self._assistant_id]):
            raise RuntimeError("Agent context not set. Cannot reload MCP servers.")

        # Reset the shared MCP middleware so it discovers the updated configuration
        from novacode_cli.mcp import reset_shared_mcp_middleware
        reset_shared_mcp_middleware()

        from novacode_cli.agents.core_agent import create_agent_with_config

        # Recreate agent, preserving state. Off the event loop for the same
        # reason as switch_model: the rebuild recompiles every subagent graph and
        # re-scans plugin skills/agents/MCP, which would otherwise freeze the TUI.
        new_agent, new_backend = await asyncio.to_thread(
            create_agent_with_config,
            model=self._model,
            assistant_id=self._assistant_id,
            tools=self._tools,
            sandbox=self._sandbox,
            sandbox_type=self._sandbox_type,
            # Same rule as the first build in main.py: Pattern A confines the
            # local shell. Omitting it dropped confinement on every rebuild.
            exec_sandbox=self._sandbox is None and self._sandbox_type == "os",
            store=self._store,
            checkpointer=self._checkpointer,
            steering_instructions=steering_instructions or [],
            session_id=getattr(self, "session_id", None),
        )

        # Update stored references
        self._agent = new_agent
        self._backend = new_backend

        return new_agent, new_backend

    # -- agent access ----------------------------------------------------------

    def get_active_agent(self, plan_mode_enabled: bool = False) -> tuple[Any, Any]:
        """Get the active agent based on plan mode.

        Args:
            plan_mode_enabled: Whether plan mode is active (from UISettings)

        Returns:
            Tuple of (agent, backend) - either plan agent or main agent
        """
        if plan_mode_enabled and self.plan_agent is not None:
            return self.plan_agent, self.plan_backend
        return self._agent, self._backend

    def clear_plan_agent(self) -> None:
        """Clear the plan agent after plan approval."""
        self.plan_agent = None
        self.plan_backend = None
        self.plan_content = None

    # -- plan content ----------------------------------------------------------

    def set_plan_content(self, plan_content: str) -> None:
        """Store current plan content from plan agent."""
        self.plan_content = plan_content

    def set_approved_plan(self, plan_content: str) -> None:
        """Store approved plan content for Nova agent execution."""
        self.approved_plan_content = plan_content

    def consume_approved_plan(self) -> str | None:
        """Get and clear the approved plan content."""
        content = self.approved_plan_content
        self.approved_plan_content = None
        return content