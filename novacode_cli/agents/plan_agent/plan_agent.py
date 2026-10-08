"""Plan-mode agent factory.

Creates an agent with PlanModeMiddleware (blocks write/execute tools outside
.nova/plans/ until exit_plan_mode is approved) and the plan_agent.jinja system
prompt that enforces the clarify → investigate → write plan → exit_plan_mode workflow.
"""

from __future__ import annotations

import logging
from typing import Any

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

# Type imports
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.pregel import Pregel

from novacode_cli.agents.model_retry import NovaModelRetryMiddleware as ModelRetryMiddleware
from novacode_cli.agents.plan_agent.plan_mode_middleware import PlanModeMiddleware

# Use Nova's optimized backend (non-hanging grep + ripgrep discovery + regex).
from novacode_cli.backends import OptimizedFilesystemBackend as FilesystemBackend
from novacode_cli.bootstrap.steering import SteeringMiddleware
from novacode_cli.config.config import settings
from novacode_cli.config.plan_mode import (
    BLOCKED_TOOLS_DISPLAY,
    RESTRICTED_WRITE_TOOLS_DISPLAY,
)
from novacode_cli.errors import is_retryable_model_error
from novacode_cli.hitl.interrupts import get_interrupt_configs
from novacode_cli.prompts import render_template
from novacode_cli.tracking.loop_guard import LoopGuardMiddleware

logger = logging.getLogger("nova.plan_agent")

__all__ = ["create_plan_agent_with_config", "get_plan_agent_system_prompt"]

# ── Async planning scouts ─────────────────────────────────────────────────
# The plan agent can dispatch read-only directory-scanning subagents
# (plan-scout-agent on the LangGraph server) in PARALLEL during investigation.
# The middleware's default system prompt tells agents to "return control to
# the user immediately after launching" — that is the wrong behavior in plan
# mode, where the plan agent must WAIT for scout reports, fold them into the
# investigation, and only then call exit_plan_mode. This override replaces it.
PLAN_SCOUT_ASYNC_PROMPT = """## Async planning scouts (remote LangGraph servers)

You can dispatch `plan-scout-agent` to scan the directory in the background
while you investigate. The scout is strictly **read-only**: it maps files,
summarizes key files, and searches for references, then returns a structured
findings report for you to synthesize.

### Tools

- `start_async_task`: Launch a scout in the background. Returns a task ID immediately.
- `check_async_task`: Get status + result of a scout run.
- `update_async_task`: Send new instructions to a running scout (interrupts and restarts it).
- `cancel_async_task`: Stop a scout that is no longer needed.
- `list_async_tasks`: List all tracked task IDs (survives context compaction).

### Scout workflow (plan mode)

1. **Decompose** the planning question into independent areas (one scout per
   subsystem, directory, or concern). Keep the scouting question narrow so
   each report is dense.
2. **Launch** one `start_async_task` per area. The description must name the
   paths to scan and the exact questions to answer.
3. **Wait and collect** — after launching, call `check_async_task` on each
   task. If it reports "running", wait and check again. Do NOT proceed to
   `exit_plan_mode` until every scout has reported.
4. **Synthesize** — fold the scout findings into your investigation. Cite
   their file paths in your plan steps.

### Critical rules (plan mode differs from normal async usage)

- **Do NOT end your turn after launching scouts.** You must collect their
  reports before presenting a plan.
- Never present a plan while a scout is still running.
- If a scout errors, retry once; if it still fails, proceed with your own
  read-only tools (read_file / ls / glob / grep) and note the gap in the plan.
- Scout reports are inputs to synthesis — integrate them, don't just append.
"""


def get_plan_agent_system_prompt(
    sandbox_type: str | None = None,
) -> str:
    """Get the system prompt for the plan-mode agent.

    Args:
        sandbox_type: Type of sandbox provider ("modal", "runloop", "daytona").
                     If None, agent is operating in local mode.

    Returns:
        The system prompt string for plan mode
    """
    if sandbox_type:
        from novacode_cli.integrations.sandbox_factory import get_default_working_dir

        working_dir = get_default_working_dir(sandbox_type)
    else:
        # In local mode with virtual_mode=True, the FilesystemBackend maps
        # virtual paths (starting with /) to the workspace root directory.
        # The LLM must use virtual paths like /file.txt, not Windows absolute
        # paths like B:\path\file.txt, because FilesystemMiddleware.validate_path
        # rejects Windows paths.
        working_dir = "/"

    return render_template(
        "plan_agent.jinja",
        working_dir=working_dir,
        blocked_tools=BLOCKED_TOOLS_DISPLAY,
        restricted_write_tools=RESTRICTED_WRITE_TOOLS_DISPLAY,
    )


def create_plan_agent_with_config(
    model: str | BaseChatModel,
    assistant_id: str,
    tools: list[BaseTool],
    *,
    sandbox: SandboxBackendProtocol | None = None,
    sandbox_type: str | None = None,
    system_prompt: str | None = None,
    auto_approve: bool = False,
    steering_instructions: list | None = None,
    checkpointer: Any = None,
    store: Any = None,
) -> tuple[Pregel, CompositeBackend]:
    """Create and configure a plan-mode agent with the specified model and tools.

    This creates an agent optimized for complex tasks that benefit from
    structured planning before execution. The agent includes:

    - AskQuestionMiddleware: For asking structured questions
    - PlanModeMiddleware: For plan creation and approval workflow
    - SkillsMiddleware: For loading skills from user/project directories

    Args:
        model: LLM model to use
        assistant_id: Agent identifier for memory storage
        tools: Additional tools to provide to agent
        sandbox: Optional sandbox backend for remote execution.
        sandbox_type: Type of sandbox provider ("modal", "runloop", "daytona")
        system_prompt: Optional custom system prompt
        auto_approve: If True, skip human-in-the-loop approvals
        checkpointer: Conversation checkpointer to share with the core agent so
            plan mode SEES the prior conversation (and persists across restarts).
            When None, a private in-memory checkpointer is used (no continuity).
        store: Durable KV store to share with the core agent (memory/learning).

    Returns:
        2-tuple of (graph, backend)
    """
    # Lazy import for tracing (speeds up startup)
    from novacode_cli.tracking.tracing import get_tracing_config, is_tracing_enabled

    tracing_enabled = False

    if is_tracing_enabled():
        tracing_enabled = True
        get_tracing_config()  # side-effect: registers config
    else:
        # Try to auto-configure from environment
        from novacode_cli.tracking.tracing import auto_configure

        config_result = auto_configure()
        if config_result.is_configured():
            tracing_enabled = True

    # Wrap model for OpenAI tracing if enabled and model is a ChatOpenAI instance
    wrapped_model = model
    if tracing_enabled and hasattr(model, "_model"):
        try:
            from langchain_openai import ChatOpenAI

            if isinstance(model, ChatOpenAI):
                from novacode_cli.tracking.tracing import (
                    wrap_openai_client as _wrap_openai,
                )

                wrapped_model = _wrap_openai(model)
        except ImportError:
            pass

    # Determine workspace root for path containment (resolves to subdirectory if applicable)
    workspace_root = settings.get_workspace_root()

    # Skills directory - global (shared across all agents at ~/.nova/skills/)
    skills_dir = settings.ensure_user_skills_dir()
    # Project-level skills directories (if in a project)
    project_skills_dirs = settings.get_project_skills_dirs()

    # Build skill sources using virtual path prefixes that match CompositeBackend routes.
    skill_sources: list[str] = ["/skills/"]
    if settings.get_shared_skills_dir().is_dir():
        skill_sources.append("/shared-skills/")
    for i, _p in enumerate(project_skills_dirs):
        skill_sources.append(f"/project-skills-{i}/")

    # CONDITIONAL SETUP: Local vs Remote Sandbox
    if sandbox is None:
        # ========== LOCAL MODE ==========
        # Backend: Local filesystem for code with virtual path semantics.
        # virtual_mode=True maps virtual paths (starting with /) to the workspace root,
        # and rejects Windows absolute paths (B:\...) and path traversal (..).
        _default_backend = FilesystemBackend(
            root_dir=str(workspace_root),
            virtual_mode=True,
        )

    else:
        # ========== REMOTE SANDBOX MODE ==========
        # Backend: Remote sandbox for code execution
        _default_backend = sandbox

    # ------------------------------------------------------------------
    # Build CompositeBackend with routes per deepagents 0.5.6 docs:
    # https://docs.langchain.com/oss/python/deepagents/backends#compositebackend-router
    #
    # SkillsMiddleware receives the same backend as create_deep_agent
    # and calls backend.ls(source_path) for each source. When source_path
    # is "/skills/", CompositeBackend routes it to the FilesystemBackend
    # rooted at ~/.nova/skills/.
    # ------------------------------------------------------------------

    _skills_backend = FilesystemBackend(
        root_dir=str(skills_dir),
        virtual_mode=True,
    )

    _routes: dict[str, BackendProtocol] = {  # type: ignore[name-defined]
        "/skills/": _skills_backend,
    }

    shared_skills_dir = settings.get_shared_skills_dir()
    if shared_skills_dir.is_dir():
        _routes["/shared-skills/"] = FilesystemBackend(
            root_dir=str(shared_skills_dir), virtual_mode=True,
        )

    # Add project-level skills routes (each gets its own FilesystemBackend)
    for i, proj_skills_dir in enumerate(project_skills_dirs):
        _proj_backend = FilesystemBackend(
            root_dir=str(proj_skills_dir),
            virtual_mode=True,
        )
        _routes[f"/project-skills-{i}/"] = _proj_backend

    # Add /memories/ route for agent directory (~/.nova/<agent>/).
    # Per deepagents docs, /memories/ is the canonical route for persistent
    # agent memory. This allows the agent's read_file tool to access
    # memory files (NOVA.md, CLAUDE.md) via virtual paths like
    # /memories/NOVA.md. AgentMemoryMiddleware reads these files directly
    # from the filesystem at startup, but the /memories/ route enables
    # the agent to re-read them during execution.
    agent_dir = settings.get_agent_dir(assistant_id)
    if agent_dir:
        _agent_backend = FilesystemBackend(
            root_dir=str(agent_dir),
            virtual_mode=True,
        )
        _routes["/memories/"] = _agent_backend

    # Add /.nova/plans/ route for plan files.
    # This allows the plan agent to write and read plan files via virtual paths
    # like /.nova/plans/plan-refactor.md. The FilesystemBackend maps these
    # to {workspace_root}/.nova/plans/ on disk.
    _plans_dir = workspace_root / ".nova" / "plans"
    _plans_dir.mkdir(parents=True, exist_ok=True)
    _plans_backend = FilesystemBackend(
        root_dir=str(_plans_dir),
        virtual_mode=True,
    )
    _routes["/.nova/plans/"] = _plans_backend

    # Add /project-memory/ route for project-level memory files.
    # This allows the plan agent to read project memory (NOVA.md, CLAUDE.md)
    # via virtual paths like /project-memory/NOVA.md.
    _project_nova_dir = workspace_root / ".nova"
    _project_nova_dir.mkdir(parents=True, exist_ok=True)
    _project_memory_backend = FilesystemBackend(
        root_dir=str(_project_nova_dir),
        virtual_mode=True,
    )
    _routes["/project-memory/"] = _project_memory_backend

    composite_backend = CompositeBackend(
        default=_default_backend,
        routes=_routes,
    )

    # Get the system prompt (sandbox-aware)
    if system_prompt is None:
        system_prompt = get_plan_agent_system_prompt(sandbox_type=sandbox_type)

    if auto_approve:
        interrupt_on = {}
    else:
        interrupt_on = get_interrupt_configs()

    # Import create_deep_agent here to avoid circular imports
    from deepagents.graph import create_deep_agent

    # Share the core agent's checkpointer + thread so plan mode continues from
    # the existing conversation (with the core agent OR a prior plan turn) and
    # survives restarts. Fall back to a private in-memory saver only when no
    # shared checkpointer is provided (e.g. standalone/tests).
    plan_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()

    # Async planning scouts: give the plan agent the async-task tools
    # (start / check / update / cancel / list) so it can dispatch read-only
    # directory-scanning subagents in parallel during investigation. The
    # default AsyncSubAgentMiddleware prompt says "return control to the user
    # immediately after launching" — wrong for plan mode, where the plan agent
    # must WAIT for scout reports before presenting the plan. Best-effort:
    # if no scouts are configured or the middleware is unavailable, the plan
    # agent falls back to its own read-only tools (no behavior change).
    plan_middleware = [
        # Retry transient model failures (rate limits / 429, timeouts,
        # network blips) with exponential backoff before erroring out.
        # Skip retries on permanent failures (usage cap / bad key) and
        # re-raise on exhaustion so the agent-loop funnel renders a clean
        # provider notice instead of hiding it in a fake AIMessage.
        ModelRetryMiddleware(
            max_retries=3,
            retry_on=is_retryable_model_error,
            on_failure="error",
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
        PlanModeMiddleware(workspace_root=workspace_root),
        # Break identical-repeat tool loops (same tool + args + result).
        # The main agent has this guard; plan mode lacked it, so a stuck
        # model could re-read the same file indefinitely while planning.
        LoopGuardMiddleware(threshold=3),
        # Keep the SHARED session list reference even when empty — using
        # `or []` would swap in a fresh list and break live steering, since
        # the list is usually empty at plan-agent creation time.
        SteeringMiddleware(
            instructions=(steering_instructions if steering_instructions is not None else [])
        ),
    ]

    try:
        from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

        from novacode_cli.agents.default_subagents.async_subagents import (
            retrieve_async_subagents,
        )

        scout_specs = retrieve_async_subagents()
        if scout_specs:
            from novacode_cli.agents.async_on_demand import AsyncServerOnDemandMiddleware

            plan_middleware.append(AsyncServerOnDemandMiddleware())
            plan_middleware.append(
                AsyncSubAgentMiddleware(
                    async_subagents=scout_specs,
                    system_prompt=PLAN_SCOUT_ASYNC_PROMPT,
                )
            )
    except Exception:  # noqa: BLE001 — never break plan mode on scout wiring
        logger.debug("async scout middleware unavailable; plan agent runs read-only", exc_info=True)

    agent = create_deep_agent(
        name=assistant_id,
        model=wrapped_model,
        skills=skill_sources,
        system_prompt=system_prompt,
        tools=tools,
        checkpointer=plan_checkpointer,
        backend=composite_backend,
        store=store,
        interrupt_on=interrupt_on,  # type: ignore
        subagents=[],  # Plan agents don't use sync subagents
        middleware=plan_middleware,
    )

    return agent, composite_backend
