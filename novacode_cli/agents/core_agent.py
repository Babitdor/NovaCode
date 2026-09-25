"""Agent management and creation for the CLI.

This module handles the creation, configuration, and management of LangGraph
deep agents for the Nova-Code CLI. It provides:

- Agent creation with custom system prompts and tool configurations
- Management of agent profiles (global and project-specific)
- Integration with middleware components (memory, skills, MCP, shell)
- Support for multiple backends (local filesystem and sandboxes)
- Agent memory management and persistence

Key Components:
- create_agent_with_config(): Create a fully configured deep agent
- list_agents(): Display available agent profiles
- reset_agent(): Reset an agent to default configuration
- Agent profiles stored in ~/.nova/agents/<name>/agent.md

The agent is built using LangGraph's Pregel architecture with:
- Planning capability via write_todos tool
- Subagent delegation via task tool
- File system access via CompositeBackend
- Middleware for memory, skills, MCP, and shell execution
- Checkpointing for conversation state persistence
"""

import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ── Patch deepagents validate_path to accept Windows paths ──────────────────
# The deepagents FilesystemMiddleware rejects Windows absolute paths (e.g.
# B:\...), but LLMs running on Windows sometimes produce them.  Convert
# Windows paths to be relative to the workspace root before validation.
import deepagents.backends.utils as _dab_utils

_original_validate = _dab_utils.validate_path


def _patched_validate_path(path: str, allowed_prefixes=None) -> str:
    """Normalize Windows absolute paths before delegating to the real validator."""
    # If the LLM produces a Windows absolute path, resolve it relative to the
    # workspace root (cwd) so the virtual filesystem backend can map it.
    m = re.match(r"^[a-zA-Z]:[/\\]", path)
    if m:
        try:
            workspace = Path.cwd()
            abs_path = Path(path)
            rel = abs_path.relative_to(workspace)
            path = "/" + rel.as_posix()
        except ValueError:
            # Not under workspace — strip drive and hope for the best
            stripped = path[m.end() :].replace("\\", "/")
            path = f"/{stripped}"
    return _original_validate(path, allowed_prefixes=allowed_prefixes)


_dab_utils.validate_path = _patched_validate_path
# ────────────────────────────────────────────────────────────────────────────

# ── Patch deepagents string replacement to tolerate a UTF-8 BOM ─────────────
# edit_file matches the model's `old_string` against the file content read in
# text mode. On Windows, files authored by some editors carry a leading UTF-8
# BOM (U+FEFF) that the read keeps but the model's `old_string` never includes,
# so the very first edit of such a file fails with "string not found" — what
# looks like an inexplicable whitespace/encoding mismatch. Stripping a leading
# BOM from the content before matching is semantically safe (the BOM is not real
# text) and fixes those edits. Patched at every import site since the symbol is
# bound by-name at module load.
_BOM = "﻿"  # U+FEFF byte-order mark
_original_psr = _dab_utils.perform_string_replacement


def _patched_perform_string_replacement(content, old_string, new_string, replace_all=False):  # noqa: ANN001, ANN201, FBT002
    if content.startswith(_BOM) and not old_string.startswith(_BOM):
        content = content.lstrip(_BOM)
    result = _original_psr(content, old_string, new_string, replace_all)
    if isinstance(result, str) and result.startswith("Error: String not found") and not replace_all:
        from novacode_cli.agents.edit_fallback import indent_tolerant_replace

        recovered = indent_tolerant_replace(content, old_string, new_string)
        if recovered is not None:
            return recovered, 1
    return result


_dab_utils.perform_string_replacement = _patched_perform_string_replacement
for _mod_name in (
    "deepagents.backends.filesystem",
    "deepagents.backends.state",
):
    try:
        _m = __import__(_mod_name, fromlist=["perform_string_replacement"])
        if hasattr(_m, "perform_string_replacement"):
            _m.perform_string_replacement = _patched_perform_string_replacement
    except Exception:  # noqa: BLE001 - best-effort; never block import
        pass
# ────────────────────────────────────────────────────────────────────────────

# ── Patch deepagents create_sub_agent to skip the wasted double compile ─────
# SubAgentMiddleware.__init__ compiles every subagent spec into a full graph,
# then create_deep_agent assigns `private_state_keys`, whose setter rebuilds
# the task tool and compiles every spec AGAIN — the first pass is thrown away
# entirely. With ~100 subagents (core + named + plugin agents) that wasted
# pass costs ~5s of agent-build time. Caching the compiled runnable ON the
# spec dict is safe: create_sub_agent(spec, state_schema) is deterministic,
# the two passes use the same spec dicts and state_schema, and Nova re-hardens
# specs into FRESH dicts on every agent build (see _harden_subagent_specs),
# so the cache never leaks a graph across builds. Dynamic response_format
# recompiles bypass the cache.
import deepagents.middleware.subagents as _dsub

_original_create_sub_agent = _dsub.create_sub_agent
_SUBAGENT_RUNNABLE_CACHE_KEY = "__nova_compiled_runnable__"


def _cached_create_sub_agent(spec, *, state_schema=None, response_format=None):  # noqa: ANN001, ANN202
    if response_format is not None:
        return _original_create_sub_agent(
            spec, state_schema=state_schema, response_format=response_format
        )
    cached = spec.get(_SUBAGENT_RUNNABLE_CACHE_KEY)
    if cached is not None and cached[0] is state_schema:
        return cached[1]
    runnable = _original_create_sub_agent(spec, state_schema=state_schema)
    spec[_SUBAGENT_RUNNABLE_CACHE_KEY] = (state_schema, runnable)
    return runnable


_dsub.create_sub_agent = _cached_create_sub_agent
# ────────────────────────────────────────────────────────────────────────────

from langchain.tools import BaseTool
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.pregel import Pregel
from langgraph.store.base import BaseStore
from deepagents import RubricMiddleware, create_deep_agent
from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from deepagents.backends.store import StoreBackend
from deepagents.middleware.subagents import SubAgent

from novacode_cli.agents.tool_offload import UNSAVED as _UNSAVED_TOOL_RESULT
from novacode_cli.agents.tool_offload import VIRTUAL_PREFIX as _CLEARED_PREFIX
from novacode_cli.agents.tool_offload import cleared_dir

from novacode_cli.agents.default_subagents.subagents import (
    _tool_name,
    retrieve_core_subagents,
)
from novacode_cli.backends import ConversationHistoryBackend
from novacode_cli.backends import OptimizedFilesystemBackend as FilesystemBackend
from novacode_cli.backends import OptimizedLocalShellBackend
from novacode_cli.agents.default_subagents.async_subagents import (
    retrieve_async_subagents,
)
from novacode_cli.config.config import (
    COLORS,
    boot_status,
    config,
    console,
    get_default_coding_instructions,
    set_agent_color,
    settings,
)
from novacode_cli.hitl.interrupts import get_interrupt_configs
from novacode_cli.integrations.sandbox_factory import get_default_working_dir
from novacode_cli.prompts import render_template
from novacode_cli.skills.curation_middleware import SkillCurationMiddleware
from novacode_cli.skills.refreshing_middleware import (
    RefreshingSkillsMiddleware,
    SubagentSkillsMiddleware,
    listing_budget,
)

# deepagents builds each subagent's SkillsMiddleware itself, which would put the
# full ~200k-char skill listing in every subagent call. Swap in the tiered one
# (no listing; suggestions from the task description + skill_search).
import deepagents.graph as _dgraph  # noqa: E402

_dgraph.SkillsMiddleware = SubagentSkillsMiddleware


def get_shared_store() -> "BaseStore":
    """Get the shared, durable store for agent/subagent memory sharing.

    Backed by SQLite at ``~/.nova/store.db`` so structured memory written via
    the store survives restarts (see :mod:`novacode_cli.memory.store`). All
    agents and subagents in a process share this one instance.

    Returns:
        The shared durable store (falls back to in-memory if SQLite is
        unavailable).
    """
    from novacode_cli.memory.store import get_durable_store

    return get_durable_store()  # type: ignore


def _extract_agent_description(agent_md_content: str) -> str:
    """Extract a description from agent.md content.

    Looks for the first substantial line of content (ignoring headers and blank lines).

    Args:
        agent_md_content: The content of the agent.md file

    Returns:
        A brief description extracted from the file, or a default message
    """
    from novacode_cli.agents.agent_file import _split

    front, body = _split(agent_md_content)
    if front.get("description"):
        return str(front["description"]).strip()[:150]
    lines = body.strip().split("\n")

    for line in lines[:10]:  # Check first 10 lines
        line = line.strip()
        # Skip empty lines, headers, and very short lines
        if line and not line.startswith("#") and len(line) > 30:
            # Truncate if too long
            if len(line) > 150:
                return line[:147] + "..."
            return line

    # Fallback: return a generic description
    return "Agent with custom system prompt and tools"


# Cache for named subagents with TTL to avoid repeated filesystem reads
_named_subagents_cache: dict[str, tuple[float, list[SubAgent]]] = {}
_NAMED_SUBAGENTS_CACHE_TTL = 60.0  # seconds


def build_named_subagents(
    assistant_id: str,
    tools: list[BaseTool],
) -> list[SubAgent]:
    """Build SubAgent specifications from all available named agents.

    Reads all agents from both global (~/.nova/agents/) and project (.nova/agents/)
    directories, excluding the current main agent, and converts them into SubAgent
    specifications that can be passed to SubAgentMiddleware.

    Uses a cache with TTL to avoid repeated filesystem reads on agent restarts.

    Args:
        assistant_id: The name of the current main agent (to exclude from subagents)
        tools: The list of tools to provide to each subagent

    Returns:
        List of SubAgent specifications ready for SubAgentMiddleware
    """
    from novacode_cli.config.config import settings

    # Check cache first
    now = time.time()
    cache_key = assistant_id
    if cache_key in _named_subagents_cache:
        cached_time, cached_value = _named_subagents_cache[cache_key]
        if now - cached_time < _NAMED_SUBAGENTS_CACHE_TTL:
            return cached_value

    subagents: list[SubAgent] = []
    all_agents = settings.get_all_agents()

    # Collect valid candidates (path existence check is fast, no I/O for content yet)
    candidates: list[tuple[str, Path, str, Path]] = []
    for agent_name, agent_dir, scope in all_agents:
        if agent_name == assistant_id:
            continue
        agent_md_path = agent_dir / "agent.md"
        if not agent_md_path.exists():
            console.print(
                f"[dim yellow]Warning: Skipping agent '{agent_name}' - no agent.md file[/dim yellow]"
            )
            continue
        candidates.append((agent_name, agent_dir, scope, agent_md_path))

    def _read_agent(
        args: tuple[str, Path, str, Path],
    ) -> tuple[str, Path, str, str] | None:
        """Read agent.md content in a worker thread. Returns None on error."""
        agent_name, agent_dir, scope, agent_md_path = args
        try:
            return (
                agent_name,
                agent_dir,
                scope,
                agent_md_path.read_text(encoding="utf-8"),
            )
        except Exception as e:
            console.print(
                f"[dim yellow]Warning: Could not read agent.md for '{agent_name}': {e}[/dim yellow]"
            )
            return None

    def _parse_color_from_content(content: str) -> str | None:
        """Extract color from YAML frontmatter without re-reading the file."""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return None
        for line in match.group(1).split("\n"):
            kv = re.match(r"^color:\s*(.+)$", line.strip())
            if kv:
                return kv.group(1).strip().strip('"').strip("'")
        return None

    # Read all agent files in parallel
    max_workers = min(len(candidates), 8) if candidates else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        read_results = list(executor.map(_read_agent, candidates))

    for result in read_results:
        if result is None:
            continue
        agent_name, agent_dir, scope, system_prompt = result
        # ~/.nova/agents/<assistant_id>/ also holds each assistant's MEMORY
        # (seeded with the "# Agent Memory" template). That is not an agent
        # definition; offering it as a subagent adds a useless `task` entry.
        if system_prompt.lstrip().startswith("# Agent Memory"):
            continue

        description = _extract_agent_description(system_prompt)

        # Parse color directly from already-loaded content (avoids second read)
        agent_color = _parse_color_from_content(system_prompt)
        if agent_color:
            set_agent_color(agent_name, agent_color)

        # A `tools:` list in the frontmatter narrows the agent to those tools
        # (MCP ones are granted in _grant_mcp_tools); absent means all of them.
        from novacode_cli.agents.agent_file import _split, agent_tools

        chosen = agent_tools(system_prompt)
        subagent: SubAgent = {
            "name": agent_name,
            "description": f"[{scope}] {description}",
            # The body only: the frontmatter is config, not instructions.
            "system_prompt": _split(system_prompt)[1] or system_prompt,
            "tools": tools if chosen is None else [t for t in tools if _tool_name(t) in chosen],
        }
        if chosen is not None:
            subagent["_nova_tool_names"] = chosen  # type: ignore[typeddict-unknown-key]
        if agent_color:
            subagent["color"] = agent_color  # type: ignore

        subagents.append(subagent)

    # Cache the result
    _named_subagents_cache[cache_key] = (now, subagents)

    return subagents


def list_agents() -> None:
    """List all available agents with detailed information."""
    agents = settings.get_all_agents()

    if not agents:
        console.print(
            f"\n[bold {COLORS['primary']}]📋 Available Agents[/bold {COLORS['primary']}]\n"
        )
        console.print("[yellow]No agents found.[/yellow]")
        console.print(
            "[dim]Agents will be created in ~/.nova/agents/ when you first use them.[/dim]",
            style=COLORS["dim"],
        )
        return

    console.print(f"\n[bold {COLORS['primary']}]📋 Available Agents[/bold {COLORS['primary']}]\n")

    for agent_name, agent_path, scope in sorted(agents, key=lambda x: (x[2], x[0])):
        # Display scope badge
        scope_badge = "🌐" if scope == "global" else "📁"
        scope_color = COLORS["accent"] if scope == "global" else COLORS["success"]

        # Agent name with icon and scope
        console.print(
            f"  {scope_badge} [bold {COLORS['primary']}]{agent_name}[/bold {COLORS['primary']}] "
            f"[dim]([{scope_color}]{scope}[/{scope_color}])[/dim]"
        )

        # Agent path
        relative_path = (
            agent_path.relative_to(Path.home())
            if agent_path.is_relative_to(Path.home())
            else agent_path
        )
        console.print(f"    [dim]Path: ~/{relative_path}[/dim]")

        # Check for agent.md existence and show summary
        agent_md = agent_path / "agent.md"
        if agent_md.exists():
            content = agent_md.read_text(encoding="utf-8")
            # Extract first line or first sentence as description
            lines = content.strip().split("\n")
            desc = ""
            for line in lines[:3]:  # Check first 3 lines
                line = line.strip()
                if line and not line.startswith("#") and len(line) > 20:
                    desc = line[:80] + "..." if len(line) > 80 else line
                    break
            if desc:
                console.print(f"    [dim]{desc}[/dim]")
        else:
            console.print("    [yellow]⚠️  (incomplete - no agent.md)[/yellow]")

        console.print()

    console.print(f"[dim]Total: {len(agents)} agent(s)[/dim]")
    console.print()


def reset_agent(agent_name: str, source_agent: str | None = None) -> None:
    """Reset an agent to default or copy from another agent."""
    agents_root = settings.get_agents_root_dir()
    agent_dir = agents_root / agent_name

    if source_agent:
        source_dir = agents_root / source_agent
        source_md = source_dir / "agent.md"

        if not source_md.exists():
            console.print(
                f"[bold red]Error:[/bold red] Source agent '{source_agent}' not found "
                "or has no agent.md"
            )
            return

        source_content = source_md.read_text(encoding="utf-8")
        action_desc = f"contents of agent '{source_agent}'"
    else:
        source_content = get_default_coding_instructions()
        action_desc = "default"

    if agent_dir.exists():
        shutil.rmtree(agent_dir)
        console.print(f"Removed existing agent directory: {agent_dir}", style=COLORS["tool"])

    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_md = agent_dir / "agent.md"
    agent_md.write_text(source_content, encoding="utf-8")

    console.print(f"✓ Agent '{agent_name}' reset to {action_desc}", style=COLORS["primary"])
    console.print(f"Location: {agent_dir}\n", style=COLORS["dim"])


def _get_shell_platform_info(sandbox_type: str | None, exec_sandbox: bool = False) -> dict:  # noqa: FBT001, FBT002
    """Return shell/platform metadata for the current execution environment.

    Sandbox environments are always Linux regardless of the host OS.
    For local execution the real OS is detected so the LLM never uses
    bash syntax on Windows or PowerShell syntax on macOS/Linux.

    When ``exec_sandbox`` is set (Pattern A, local mode), a note is added so the
    model knows shell writes are confined to the workspace.

    Returns a dict with keys:
        platform    – "windows" | "macos" | "linux"
        shell_name  – "PowerShell" | "zsh" | "bash"
        path_sep    – "\\\\" | "/"
        shell_notes – list of platform-specific shell rules (strings)
    """
    import platform as _platform

    def _with_confinement(info: dict) -> dict:
        if exec_sandbox and not sandbox_type:
            info["shell_notes"] = [
                *info["shell_notes"],
                "The shell is confined by an OS sandbox: filesystem writes are "
                "restricted to the workspace. Writes outside it (e.g. /etc, $HOME) "
                "will fail — keep file changes inside the project.",
            ]
        return info

    if sandbox_type:
        # All supported sandbox providers (Modal, Runloop, Daytona) are Linux
        return {
            "platform": "linux",
            "shell_name": "bash",
            "path_sep": "/",
            "shell_notes": [
                "Use `bash` syntax — the sandbox is a Linux environment.",
                "Chain commands with `&&`.",
                "Use forward slashes in all paths.",
            ],
        }

    system = _platform.system().lower()

    if system == "windows":
        return _with_confinement(
            {
                "platform": "windows",
                "shell_name": "PowerShell",
                "path_sep": "\\",
                "shell_notes": [
                    "The user is on **Windows** — always use PowerShell syntax, NEVER bash.",
                    "Use `;` to chain commands (not `&&`, which is unreliable in older PowerShell).",
                    "Use `$env:VAR` for environment variables, not `$VAR` or `export VAR=`.",
                    "Use backslashes in Windows paths, or wrap paths in double quotes.",
                    "Do NOT use `rm -rf`, `cat`, `chmod`, `sudo`, `which`, or any Unix-only commands.",
                    "Use `Get-ChildItem` instead of `ls`/`find`; `Select-String` instead of `grep`.",
                    "Use `Remove-Item -Recurse -Force` instead of `rm -rf`.",
                ],
            }
        )

    if system == "darwin":
        return _with_confinement(
            {
                "platform": "macos",
                "shell_name": "zsh",
                "path_sep": "/",
                "shell_notes": [
                    "The user is on **macOS** — the default shell is `zsh`.",
                    "Chain commands with `&&`.",
                    "Use forward slashes in all paths.",
                    "Prefer `brew` for package management when available.",
                ],
            }
        )

    return _with_confinement(
        {
            "platform": "linux",
            "shell_name": "bash",
            "path_sep": "/",
            "shell_notes": [
                "The user is on **Linux** — use `bash` syntax.",
                "Chain commands with `&&`.",
                "Use forward slashes in all paths.",
            ],
        }
    )


def get_system_prompt(
    assistant_id: str,
    sandbox_type: str | None = None,
    exec_sandbox: bool = False,  # noqa: FBT001, FBT002
) -> str:
    """Get the base system prompt for the agent.

    Args:
        assistant_id: The agent identifier for path references
        sandbox_type: Type of sandbox provider ("modal", "runloop", "daytona").
                     If None, agent is operating in local mode.
        exec_sandbox: When True (Pattern A, local mode), note in the prompt that
                     shell writes are confined to the workspace.

    Returns:
        The system prompt string (without NOVA.md content)
    """
    shell_info = _get_shell_platform_info(sandbox_type, exec_sandbox=exec_sandbox)

    return render_template("core_agent_system.jinja", **shell_info)


def _harden_subagent_specs(
    specs: list,
    skill_sources: list[str] | None = None,
    main_model_supports_images: bool = False,
) -> list:
    """Return resilient, unattended copies of the subagent specs.

    Returns a NEW list of NEW spec dicts — it must NOT mutate the inputs, because
    ``build_named_subagents``/``retrieve_core_subagents`` hand back **cached**
    spec dicts that are reused across every ``create_agent_with_config`` call
    (the session agent, then /init's dedicated agent, …). Mutating them in place
    accumulated a fresh ``ModelRetryMiddleware`` per call, so the 2nd build saw
    two retry middleware with the same ``.name`` and langchain aborted with
    "Please remove duplicate middleware instances" (this is what made /init fall
    back to the shared agent).

    For each dict spec that isn't a compiled/remote subagent (``runnable``/``url``):

    - **Clear ``interrupt_on``** — a HITL interrupt raised inside a subagent
      bubbles out of the ``task`` tool as an unresolvable ``GraphInterrupt`` and
      crashes the turn (see the comment in :func:`create_agent_with_config`). The
      main agent stays the sole HITL boundary; dispatched subagents never prompt.
    - **Prepend a fresh ``ModelRetryMiddleware``** — deepagents gives subagents
      only ``[TodoList, Filesystem, Summarization, PatchToolCalls] + skills +
      spec["middleware"]``, i.e. **no retry**, while the main agent has one. So a
      transient provider 5xx/429/timeout during a subagent's model call would
      otherwise kill it (this is what broke /init's semantic-extraction
      subagents — the chunk fragment never got written). A fresh instance per
      build; skipped if the spec already carries a retry middleware.
    - **Give skills + curation** — Nova's subagent specs declare no ``skills``,
      so deepagents builds them with NO ``SkillsMiddleware`` (subagents had zero
      skills). We set ``spec["skills"] = skill_sources`` (the same virtual
      ``/skills/`` routes the main agent uses, resolved through the shared
      backend) so deepagents loads the full set, then append a fresh
      ``SkillCurationMiddleware`` — which runs after that loader — so each
      subagent sees ONLY the user-enabled (curated) skills, just like the main
      agent. Skipped if the spec already declares skills / carries curation.
    """
    from langchain.agents.middleware import ModelRetryMiddleware
    from novacode_cli.agents.agent_mailbox import read_agent_messages, send_agent_message
    from novacode_cli.bootstrap import VisionCaptionMiddleware
    from novacode_cli.errors import is_retryable_model_error
    from novacode_cli.security.middleware import SecurityMiddleware
    from novacode_cli.tracking.loop_guard import LoopGuardMiddleware

    # Tools that raise a HITL ``interrupt()`` directly in their body (not via
    # ``interrupt_on``). Clearing ``interrupt_on`` below doesn't neutralise these,
    # so they must be removed from a subagent's tool list outright — otherwise a
    # subagent invoking one raises an unresolvable ``GraphInterrupt`` that crashes
    # the turn. The main agent keeps them; it's the sole HITL boundary.
    _interrupting_tools = {"ask_user_question", "enter_plan_mode", "exit_plan_mode"}

    # Agent-to-agent: give each subagent the async-task tools (start / update /
    # check / cancel / list) so a SUBAGENT — not only the orchestrator — can
    # launch, message, and poll remote agents on the LangGraph server over the
    # Agent Protocol. One shared middleware instance carries the tools + the
    # `async_tasks` state schema (merged into each subagent graph). Built once,
    # best-effort: if no async agents are configured, subagents are unchanged.
    _async_mw = None
    try:
        from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

        _async_specs = retrieve_async_subagents()
        if _async_specs:
            _async_mw = AsyncSubAgentMiddleware(async_subagents=_async_specs)
    except Exception:  # noqa: BLE001 — never break the subagent build on this
        _async_mw = None

    out: list = []
    for spec in specs:
        if not isinstance(spec, dict) or "runnable" in spec or "url" in spec:
            out.append(spec)  # compiled/remote subagents own their own config
            continue
        existing = list(spec.get("middleware") or [])
        new_spec = dict(spec)  # copy — never mutate the (possibly cached) original
        new_spec["interrupt_on"] = {}
        spec_tools = new_spec.get("tools")
        if spec_tools:
            # New list (shallow copy shares the ref with the main tool list).
            new_spec["tools"] = [
                t for t in spec_tools if getattr(t, "name", None) not in _interrupting_tools
            ]
        # Agent-to-agent mailbox: peer messaging between subagents. Append the
        # tools (deduped) and tell the subagent its own name so it can address
        # its inbox (as_agent) and sign its messages (from_agent).
        _sa_name = new_spec.get("name") or "subagent"
        _cur_tools = list(new_spec.get("tools") or [])
        _have = {getattr(t, "name", None) for t in _cur_tools}
        for _mb in (send_agent_message, read_agent_messages):
            if _mb.name not in _have:
                _cur_tools.append(_mb)
        new_spec["tools"] = _cur_tools
        new_spec["system_prompt"] = (
            (new_spec.get("system_prompt") or "")
            + f"\n\nYour agent name is '{_sa_name}'. For agent-to-agent messaging use "
            f"send_agent_message(to_agent=<peer>, message=..., from_agent='{_sa_name}') "
            f"and read_agent_messages(as_agent='{_sa_name}')."
        )

        has_retry = any(type(m).__name__ == "ModelRetryMiddleware" for m in existing)
        has_vision = any(type(m).__name__ == "VisionCaptionMiddleware" for m in existing)
        has_security = any(type(m).__name__ == "SecurityMiddleware" for m in existing)
        has_curation = any(type(m).__name__ == "SkillCurationMiddleware" for m in existing)
        has_loopguard = any(type(m).__name__ == "LoopGuardMiddleware" for m in existing)
        has_async = any(type(m).__name__ == "AsyncSubAgentMiddleware" for m in existing)

        # Give the subagent the (full) skill sources so deepagents attaches a
        # SkillsMiddleware on the shared backend — unless the spec already
        # declares its own skills. SkillCurationMiddleware (appended below, after
        # the loader) then clamps them to the user-enabled set.
        if skill_sources and "skills" not in new_spec:
            new_spec["skills"] = list(skill_sources)

        mw_to_add = []
        if not has_retry:
            mw_to_add.append(
                ModelRetryMiddleware(
                    max_retries=3,
                    retry_on=is_retryable_model_error,
                    on_failure="error",
                    backoff_factor=2.0,
                    initial_delay=1.0,
                )
            )
        if not has_vision:
            mw_to_add.append(
                VisionCaptionMiddleware(
                    main_model_supports_images=main_model_supports_images
                )
            )
        if not has_security:
            mw_to_add.append(SecurityMiddleware())
        # Break identical-repeat tool loops inside subagents too. Only the MAIN
        # agent had a LoopGuard, so a stuck subagent (e.g. re-reading the same
        # SKILL.md forever) looped unguarded until its recursion limit. Fresh
        # instance per spec — the guard keeps per-instance history state.
        if not has_loopguard:
            mw_to_add.append(LoopGuardMiddleware(threshold=3))
        # Agent-to-agent tools (see _async_mw above): the subagent can now message
        # remote agents on the LangGraph server, not just the orchestrator.
        if _async_mw is not None and not has_async:
            mw_to_add.append(_async_mw)
        # Only curate when the subagent actually has skills (either just granted
        # above or pre-declared by the spec) — no point adding a no-op clamp.
        if new_spec.get("skills") and not has_curation:
            # Lives in spec["middleware"], which deepagents.graph appends AFTER
            # the SkillsMiddleware loader — so it sees the loaded skills_metadata
            # and clamps it. A fresh instance per subagent.
            mw_to_add.append(SkillCurationMiddleware())

        new_spec["middleware"] = mw_to_add + existing
        out.append(new_spec)
    return out


def _seed_summarization_profile(
    model: object, model_name: str, window: int | None = None
) -> None:
    """Seed ``model.profile['max_input_tokens']`` so deepagents' built-in
    ``SummarizationMiddleware`` triggers on OUR context budget.

    deepagents auto-computes the summarization trigger from the model profile:
    with a profile exposing ``max_input_tokens`` it summarizes at 85% of the
    window (keeping the last ~10%); WITHOUT one it falls back to a fixed
    170k-token trigger — which never fires for local/Ollama models whose real
    window is far smaller (the model just overflows). Seeding the profile from
    the same window Nova uses for ``/context`` ties the summarization trigger to
    the displayed ctx %.

    The window is ALWAYS overwritten, even when the model ships its own profile.
    A model-supplied number (e.g. ChatOpenAI's 128K for gpt-4o) can disagree with
    the window Nova computes and displays — and then the library summarizes on
    one budget while the ctx% indicator reports another, so compaction appears to
    fire "for no reason". Both must measure the same window; other profile keys
    (max_output_tokens, …) are preserved.

    Nova's own auto-compaction runs first, at
    :data:`~novacode_cli.context._analysis.AUTO_COMPACT_THRESHOLD` (0.82), below
    the library's 0.85 — so this middleware only fires as a mid-turn backstop.

    No-op when the model isn't a chat-model instance. Never raises.

    Args:
        model: The resolved chat model whose profile is seeded.
        model_name: The bound model's name, used to resolve the window when the
            caller does not already have it.
        window: The bound model's effective window, if the caller already
            resolved it. Supplying it avoids a second probe — for local Ollama
            models that means a second ``ollama ps`` subprocess (uncached, 10s
            timeout), since ``create_agent_with_config`` needs the same window
            for the context-editing trigger.
    """
    try:
        if not isinstance(model, BaseChatModel):
            return
        if window is None:
            from novacode_cli.context import ContextManager

            window = ContextManager(model_name).window_size()
        if window and window > 0:
            existing = getattr(model, "profile", None)
            profile = dict(existing) if isinstance(existing, dict) else {}
            profile["max_input_tokens"] = int(window)
            model.profile = profile  # type: ignore[assignment]
    except Exception:  # noqa: BLE001 - never block agent build on a profile hint
        __import__("logging").getLogger(__name__).debug(
            "Could not seed model profile for summarization", exc_info=True
        )


def _declare_image_support(model: object) -> None:
    """Record on the model's profile that it accepts images.

    deepagents scrubs image blocks out of a ``read_file`` result whenever the
    bound model's ``ModelProfile`` has ``image_inputs: False``, replacing them
    with "[read_file: X was not attached because this model does not support
    image content]". That check reads the PROFILE — so Nova deciding the model
    is multimodal (pattern match, or the user running ``/vision on``) was not
    enough on its own: the image was still stripped before the model saw it,
    and the model, told only that something was withheld, would go and write a
    script to decode the pixels instead.

    Only called when Nova has concluded the model can see images. Never raises.
    """
    try:
        if not isinstance(model, BaseChatModel):
            return
        existing = getattr(model, "profile", None)
        profile = dict(existing) if isinstance(existing, dict) else {}
        if profile.get("image_inputs") is True:
            return
        profile["image_inputs"] = True
        model.profile = profile  # type: ignore[assignment]
    except Exception:  # noqa: BLE001 — never block an agent build on a profile hint
        __import__("logging").getLogger(__name__).debug(
            "Could not declare image support on the model profile", exc_info=True
        )


def _build_skill_sources() -> tuple[list[str], Path, Path, list[Path], list[tuple[str, Path]]]:
    """Assemble the skill source prefixes and the directories backing them.

    Returns:
        5-tuple of (skill_sources, skills_dir, claude_skills_dir,
        project_skills_dirs, plugin_skills).
    """
    skill_sources: list[str] = []

    # Skills directory - global (shared across all agents at ~/.nova/skills/)
    skills_dir = settings.ensure_user_skills_dir()
    # Global Claude Code skills directory (~/.claude/skills/)
    claude_skills_dir = settings.get_global_claude_skills_dir()
    # Project-level skills directories (if in a project)
    # Supports both .claude/skills/ and .nova/skills/
    project_skills_dirs = settings.get_project_skills_dirs()

    # Build skill sources using virtual path prefixes that match CompositeBackend routes.
    # This ensures SkillsMiddleware.ls("/skills/") routes to the FilesystemBackend
    # instead of the default backend (which may fail outside graph context).
    skill_sources.append("/skills/")
    if claude_skills_dir.exists():
        skill_sources.append("/claude-skills/")
    for i, _p in enumerate(project_skills_dirs):
        skill_sources.append(f"/project-skills-{i}/")

    # Skills from installed Claude-compatible plugins (~/.nova/plugins/*/skills).
    from novacode_cli.plugins.claude_plugins import plugin_skill_dirs

    plugin_skills = plugin_skill_dirs()
    for pname, _d in plugin_skills:
        skill_sources.append(f"/plugin-skills-{pname}/")

    return skill_sources, skills_dir, claude_skills_dir, project_skills_dirs, plugin_skills


def _host_drive_roots(workspace_root: Path) -> list[str]:
    """Filesystem roots to mount so real absolute paths resolve.

    Windows: every fixed drive present (``C:/``, ``B:/``, …) — the user's files
    are routinely on a different drive from the workspace. POSIX: just ``/``.
    Probing drives is cheap (``Path("X:/").exists()``) and done once at build.
    """
    import contextlib

    if os.name != "nt":
        return ["/"]

    roots: list[str] = []
    # The workspace's own drive first — the common case, and guaranteed to
    # exist even if the probe below is restricted in some environment.
    with contextlib.suppress(Exception):
        anchor = Path(workspace_root).anchor.replace("\\", "/")
        if anchor:
            roots.append(anchor if anchor.endswith("/") else f"{anchor}/")

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:/"
        if root in roots:
            continue
        with contextlib.suppress(OSError):
            if Path(root).exists():
                roots.append(root)
    return roots


def _build_composite_backend(
    *,
    sandbox: SandboxBackendProtocol | None,
    sandbox_type: str | None,
    workspace_root: Path,
    skills_dir: Path,
    claude_skills_dir: Path,
    project_skills_dirs: list[Path],
    plugin_skills: list[tuple[str, Path]],
    agent_dir: Path | None,
    store: BaseStore | None,
    assistant_id: str,
    session_id: str | None = None,
) -> tuple[CompositeBackend, BackendProtocol]:
    """Pick the default backend (local vs remote sandbox) and build the
    CompositeBackend with all virtual-path routes.

    Returns:
        2-tuple of (composite_backend, default_backend).
    """
    # CONDITIONAL SETUP: Local vs Remote Sandbox
    if sandbox is None:
        # ========== LOCAL MODE ==========
        # Default backend must support command execution for subagents: deepagents'
        # FilesystemMiddleware always registers an `execute` tool, and declarative
        # subagents inherit that tool but do not get Nova's ShellMiddleware. In
        # local mode the previous default (a plain FilesystemBackend) caused the
        # `execute` tool to fail with "Default backend doesn't support command
        # execution". We use OptimizedLocalShellBackend as the default instead: a
        # LocalShellBackend subclass that also mixes in OptimizedFilesystemBackend,
        # so project reads/writes keep their virtual-root semantics, the subagent's
        # `execute` tool can run commands locally (SandboxBackendProtocol), AND grep
        # uses Nova's guarded, non-hanging ripgrep search (a plain LocalShellBackend
        # would hit deepagents' base grep, which crashes on Windows when ripgrep
        # returns stdout=None — "'NoneType' object has no attribute 'splitlines'").
        _default_backend = OptimizedLocalShellBackend(
            root_dir=str(workspace_root),
            virtual_mode=True,
            env=dict(os.environ),
        )

    else:
        # ========== REMOTE SANDBOX MODE ==========
        # Backend: Remote sandbox for code execution.
        # Wrap it so the agent's `/`-rooted *virtual* project paths (e.g.
        # `/novacode_cli/x`) map to the sandbox **working directory** (e.g.
        # `/workspace/novacode_cli/x`) for file ops. Without this, file reads
        # hit the container root and 404, even though `execute` runs in the
        # workdir — see novacode_cli/integrations/workdir_backend.py.
        from novacode_cli.integrations.workdir_backend import WorkdirSandboxBackend

        _sandbox_workdir = None
        if sandbox_type:
            try:
                _sandbox_workdir = get_default_working_dir(sandbox_type)
            except Exception:  # noqa: BLE001
                _sandbox_workdir = None
        if _sandbox_workdir is None:
            _sandbox_workdir = getattr(sandbox, "_workdir", None) or "/workspace"
        _default_backend = WorkdirSandboxBackend(sandbox, workdir=_sandbox_workdir)  # type: ignore

    # ------------------------------------------------------------------
    # Build CompositeBackend with routes per deepagents 0.5.6 docs:
    # https://docs.langchain.com/oss/python/deepagents/backends#compositebackend-router
    #
    # CompositeBackend routes file operations to different backends based
    # on path prefix. SkillsMiddleware receives the same backend as
    # create_deep_agent and calls backend.ls(source_path) for each source.
    # When source_path is "/skills/", CompositeBackend routes it to the
    # FilesystemBackend rooted at ~/.nova/skills/.
    #
    # Canonical pattern (from deepagents docs and examples):
    #   skill_backend = FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True)
    #   backend = CompositeBackend(
    #       default=StateBackend(),
    #       routes={
    #           "/memories/": StoreBackend(),
    #           "/skills/": skill_backend,
    #       },
    #   )
    #   create_deep_agent(backend=backend, skills=["/skills/"], ...)
    # ------------------------------------------------------------------

    _skills_backend = FilesystemBackend(
        root_dir=str(skills_dir),
        virtual_mode=True,
    )

    _routes: dict[str, BackendProtocol] = {  # type: ignore[name-defined]
        "/skills/": _skills_backend,
    }

    # Add global Claude Code skills route (~/.claude/skills/)
    if claude_skills_dir.exists():
        _claude_skills_backend = FilesystemBackend(
            root_dir=str(claude_skills_dir),
            virtual_mode=True,
        )
        _routes["/claude-skills/"] = _claude_skills_backend

    # Add project-level skills routes (each gets its own FilesystemBackend)
    for i, proj_skills_dir in enumerate(project_skills_dirs):
        _proj_backend = FilesystemBackend(
            root_dir=str(proj_skills_dir),
            virtual_mode=True,
        )
        _routes[f"/project-skills-{i}/"] = _proj_backend

    # Installed-plugin skills routes (mirrors project skills; plugin_skills
    # gathered in _build_skill_sources so sources and routes stay in lockstep).
    for pname, plugin_skills_dir in plugin_skills:
        _routes[f"/plugin-skills-{pname}/"] = FilesystemBackend(
            root_dir=str(plugin_skills_dir),
            virtual_mode=True,
        )

    # Add /memories/ route for agent directory (~/.nova/<agent>/).
    # Per deepagents docs, /memories/ is the canonical route for persistent
    # agent memory. This allows the agent's read_file tool to access
    # memory files (NOVA.md, CLAUDE.md) via virtual paths like
    # /memories/NOVA.md. AgentMemoryMiddleware reads these files directly
    # from the filesystem at startup, but the /memories/ route enables
    # the agent to re-read them during execution.
    if agent_dir:
        _agent_backend = FilesystemBackend(
            root_dir=str(agent_dir),
            virtual_mode=True,
        )
        _routes["/memories/"] = _agent_backend

    # /cleared/ route → offloaded tool results (see agents/tool_offload.py).
    # A cleared result's placeholder names a path under this prefix; without the
    # route the model would be told to read a file it cannot open.
    if agent_dir:
        _cleared = cleared_dir(agent_dir)
        _cleared.mkdir(parents=True, exist_ok=True)
        _routes[_CLEARED_PREFIX] = FilesystemBackend(
            root_dir=str(_cleared),
            virtual_mode=True,
        )

    # /store/ route → StoreBackend (persistent, cross-thread).
    #
    # Unlike /memories/ (which is backed by a FilesystemBackend so
    # AgentMemoryMiddleware can read agent.md directly from disk), /store/
    # stores files in LangGraph's durable Store (SQLite at ~/.nova/store.db).
    # Data written here survives agent restarts, thread switches, and is
    # shared across subagents.
    #
    # The namespace is scoped by (assistant_id, "store") so different agents
    # have isolated storage.  Use this for cross-session data that the agent
    # wants to keep — preference records, accumulated facts, etc.
    if store is not None:
        _ns = assistant_id  # captured in closure for namespace factory
        _store_backend = StoreBackend(
            store=store,
            namespace=lambda rt: (_ns, "store"),  # type: ignore[union-attr]
        )
        _routes["/store/"] = _store_backend

    # Add /.nova/plans/ route for plan files.
    # This allows the agent to write and read plan files via virtual paths
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
    # This allows the agent to read/write project memory (NOVA.md, CLAUDE.md)
    # via virtual paths like /project-memory/NOVA.md. The FilesystemBackend
    # maps these to {workspace_root}/.nova/ on disk.
    _project_nova_dir = workspace_root / ".nova"
    _project_nova_dir.mkdir(parents=True, exist_ok=True)
    _project_memory_backend = FilesystemBackend(
        root_dir=str(_project_nova_dir),
        virtual_mode=True,
    )
    _routes["/project-memory/"] = _project_memory_backend

    # Add /large_tool_results/ route → global per-session folder.
    # deepagents' FilesystemMiddleware offloads oversized tool results to
    # /large_tool_results/<tool_call_id>. Without this route those writes fall
    # through to the workspace-rooted default backend and litter the *project*
    # directory. Route them to ~/.nova/sessions/<session_id>/large_tool_results/
    # so offloaded results live with the rest of the session's state, not the repo.
    _large_results_dir = (
        Path.home() / ".nova" / "sessions" / (session_id or "default") / "large_tool_results"
    )
    _large_results_dir.mkdir(parents=True, exist_ok=True)
    _routes["/large_tool_results/"] = FilesystemBackend(
        root_dir=str(_large_results_dir),
        virtual_mode=True,
    )

    # Same treatment for /conversation_history/ — the summarization middleware
    # offloads evicted conversation history there, and without a route it too
    # leaks into the project directory.
    #
    # Write-only on purpose (ConversationHistoryBackend refuses reads): the
    # summarizer tells the model it can "recover the full text by reading the
    # offloaded file", and following that re-inhales the context eviction just
    # cleared, re-triggering summarization — the repeating SESSION INTENT loop.
    # The file is still written, so the transcript survives for the user.
    _conv_history_dir = (
        Path.home() / ".nova" / "sessions" / (session_id or "default") / "conversation_history"
    )
    _conv_history_dir.mkdir(parents=True, exist_ok=True)
    _routes["/conversation_history/"] = ConversationHistoryBackend(
        root_dir=str(_conv_history_dir),
        virtual_mode=True,
    )

    # Real absolute host paths (local mode only). The model constantly passes a
    # path it can see — a sibling project, a file the user named, something from
    # an error trace — and deepagents refused every one of them outright:
    #
    #   Windows absolute paths are not supported: B:/…/ai-job-search/docs/CV.pdf
    #
    # Paths inside the workspace are rewritten to virtual form (host_path.py) and
    # were always fine; anything one directory over was unreachable, for reads
    # AND writes. Mounting each drive root serves those paths for real, the same
    # way Claude Code takes real paths — access is gated by the approval policy
    # (write_file/edit_file default to "ask"; system/secret globs are denied),
    # not by refusing to name the file.
    #
    # These routes sort SHORTEST, so every "/…" virtual route above still wins;
    # only a drive-letter path reaches them. Remote/sandbox mode is skipped —
    # there the host filesystem is not the agent's filesystem.
    _drive_routes = _host_drive_roots(workspace_root) if sandbox is None else []
    if sandbox is None:
        for _drive in _drive_routes:
            _routes.setdefault(
                _drive, FilesystemBackend(root_dir=_drive, virtual_mode=True)
            )

    composite_backend = CompositeBackend(
        default=_default_backend,
        routes=_routes,
    )
    # A grep/glob with no path (or "/") fans out to every entry in `.routes` —
    # which made each path-less search walk entire drives (B:\, C:\) and time
    # out, and a route's timeout fails the WHOLE search even when the project
    # part succeeded. `.routes` feeds only that fan-out; explicit paths route
    # through `.sorted_routes`, so a real absolute path still resolves.
    composite_backend.routes = {
        prefix: backend for prefix, backend in _routes.items() if prefix not in _drive_routes
    }

    return composite_backend, _default_backend


# What an old, cleared tool result reads as when its payload could NOT be
# written to disk. The normal case is the restorable one — see
# novacode_cli/agents/tool_offload.py.
CLEARED_TOOL_RESULT = _UNSAVED_TOOL_RESULT

# Marker the offloading edit uses to find the messages it just cleared, so it
# must differ from every final placeholder text.
_CLEARING_MARKER = "[cleared]"


def _tool_result_clearing(context_window: int, offload_dir: Path | None = None):  # noqa: ANN202
    """The tool-result clearing edit Nova runs before whole-history compaction.

    Restorable: the payload is written to ``offload_dir`` and the placeholder
    names it, so clearing costs context but never information. Without an
    offload directory it degrades to the plain "re-run the tool" placeholder.
    """
    from novacode_cli.agents.tool_offload import OffloadingToolUsesEdit

    return OffloadingToolUsesEdit(
        trigger=_context_edit_trigger(context_window),
        keep=5,  # keep last 5 tool results
        clear_tool_inputs=False,
        # read_file is NOT excluded: file reads are the largest results in a
        # coding session, and an old one is exactly the observation masking
        # should drop (Anthropic's context editing, JetBrains' "Complexity
        # Trap"). The file is still on disk, and the read-before-edit guard
        # checks the tracker, not message content, so a re-read is the fix.
        #
        # web_search is no longer excluded either: it was excluded only because
        # a search is the one result you CANNOT re-run deterministically — and
        # now it isn't dropped, it's offloaded. `think` stays excluded: it is
        # the model's own reasoning, not an observation, and it is small.
        exclude_tools=["think"],
        placeholder=_CLEARING_MARKER if offload_dir else CLEARED_TOOL_RESULT,
        offload_dir=offload_dir,
    )


def _context_edit_trigger(context_window: int, *, fallback: int = 60_000) -> int:
    """Resolve ``ClearToolUsesEdit.trigger`` (absolute tokens) from a window.

    The library's trigger is an ``int`` token count, so a fraction cannot be
    passed through directly — it is resolved here, once, at agent-build time.
    Floored so a tiny window still leaves room for the retained recent results,
    and falls back to the legacy fixed count when the window is unknown.
    """
    if not context_window or context_window <= 0:
        return fallback
    from novacode_cli.context import CONTEXT_EDIT_TRIGGER_FRACTION, compact_threshold_pct

    # Never at or above the compaction point: clearing is the cheap reducer and
    # has to get its chance first. On a window under ~16K the compaction point
    # is driven down by the absolute headroom reserve, which would otherwise
    # overtake a flat 60% and invert the two stages.
    # The floor is a SHARE of the window, not an absolute count: an absolute
    # floor can sit above the compaction point on a small window and invert the
    # two stages again.
    compact_at = int(context_window * compact_threshold_pct(context_window) / 100)
    return max(
        int(context_window * 0.25),
        min(int(context_window * CONTEXT_EDIT_TRIGGER_FRACTION), compact_at - 2_000),
    )


def _resolve_main_model_multimodal(model: str | BaseChatModel) -> bool:
    """Whether the main model can accept image input (see model_capabilities).

    Thin wrapper over :func:`~novacode_cli.config.model_capabilities.
    resolve_main_model_multimodal`, which is the single source of truth. It is
    shared with prompt preparation so the middleware and the ingestion path
    cannot disagree about whether a pasted image should be captioned.
    """
    from novacode_cli.config.model_capabilities import resolve_main_model_multimodal

    return resolve_main_model_multimodal(model)


def _build_middleware_stack(
    *,
    model: str | BaseChatModel,
    assistant_id: str,
    store: BaseStore | None,
    skills_dir: Path,
    agent_dir: Path | None,
    workspace_root: Path,
    steering_instructions: list | None,
    composite_backend: CompositeBackend,
    sandbox: SandboxBackendProtocol | None,
    sandbox_type: str | None,
    exec_sandbox: bool,  # noqa: FBT001
    context_window: int = 0,
    main_model_supports_images: bool = False,
) -> list:
    """Build the core agent middleware stack.

    The ORDER of this list is load-bearing — see the inline comments on each
    entry. Callers may append/insert further middleware (plugins, MCP, skills)
    around this base stack.

    Args:
        context_window: The bound model's effective context window in tokens,
            used to size the window-relative context-editing trigger. 0 (or
            negative) falls back to the legacy fixed trigger.
    """
    # Lazy imports for middleware (speeds up startup)
    from langchain.agents.middleware import ModelRetryMiddleware

    from novacode_cli.agents.task_discipline import TaskDisciplineMiddleware
    from novacode_cli.errors import is_retryable_model_error
    from novacode_cli.bootstrap import (
        BootstrapMiddleware,
        VisionCaptionMiddleware,
    )
    from novacode_cli.bootstrap.steering import SteeringMiddleware
    from novacode_cli.hermes.middleware import NovaLearningMiddleware
    from novacode_cli.memory.agent_memory import AgentMemoryMiddleware
    from novacode_cli.security.middleware import SecurityMiddleware
    from novacode_cli.shell import ShellMiddleware
    from novacode_cli.tracking.file_tracker import FileTrackerMiddleware
    from novacode_cli.tracking.loop_guard import LoopGuardMiddleware
    from langchain.agents.middleware import (
        ContextEditingMiddleware,
    )

    # Hermes learning loop is opt-in (persisted config). Off by default so the
    # periodic review + skill-creation LLM calls don't run unless the user asks.
    from novacode_cli.config.nova_config import NovaConfig

    _learning_enabled = NovaConfig().get_learning_enabled()

    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore")  # beta-API warning would print every boot
        _rubric_middleware = RubricMiddleware(model=model, max_iterations=3)

    agent_middleware = [
        # Retry transient model failures (rate limits / 429, timeouts, network
        # blips) with exponential backoff before surfacing an error to the user.
        # retry_on skips *permanent* failures (usage/quota cap, bad API key) so
        # they surface at once instead of after 4 pointless waits. on_failure=
        # "error" RE-RAISES once retries are exhausted (the default "continue"
        # would hide the failure inside a fake AIMessage); the re-raised
        # exception then hits the funnel in iterate_agent_events, which renders a
        # clean provider notice via friendly_model_error in both UIs.
        ModelRetryMiddleware(
            max_retries=3,
            retry_on=is_retryable_model_error,
            on_failure="error",
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
        # Vision captioning — converts images to TEXT so a text-only main model
        # never receives image blocks. A read_file on an image is captioned by the
        # vision model at the tool-result layer (awrap_tool_call); pasted images
        # are captioned upstream at ingestion. awrap_model_call here is a pure
        # safety net that strips any residual image blocks from history.
        #
        # When the MAIN model is multimodal (main_model_supports_images), this
        # middleware is a pass-through: images go straight to the main model,
        # which reads them directly — no auxiliary vision model required.
        VisionCaptionMiddleware(main_model_supports_images=main_model_supports_images),
        # Nova — autonomous learning middleware that tracks tool usage,
        # triggers periodic review cycles, and manages memory tiers.
        # Positioned after ModelRetryMiddleware so retried tool calls don't
        # inflate the counter, but before other middleware so learning data
        # is collected for all downstream operations.
        #
        # The Hermes learning loop (periodic review + skill creation) fires
        # out-of-band LLM calls every N tool calls. It is OFF by default to keep
        # per-turn LLM cost and latency minimal; users opt in via the persisted
        # `learning_enabled` config (NovaConfig). When disabled, the middleware
        # is a no-op tracker (no LLM calls).
        NovaLearningMiddleware(
            store=store,  # type: ignore
            skills_dir=skills_dir,
            agent_dir=agent_dir,
            enabled=_learning_enabled,
        ),
        # Screen URL-bearing tool args for deceptive Unicode / spoofed domains
        # (warn + sanitize) before the tool runs. Early in the stack so the
        # cleaned args flow to every downstream middleware and the tool itself.
        SecurityMiddleware(),
        BootstrapMiddleware(workspace_root=str(workspace_root)),
        # Connect to the session's shared steering list so /steer AND live
        # mid-run steering (TUI) reach the model — the middleware reads this
        # list on every model call, so appends made while the agent is working
        # take effect on its next step.
        SteeringMiddleware(instructions=steering_instructions),
        FileTrackerMiddleware(
            # Read-before-edit rejection disabled — it blocked too many valid
            # edits (edit_file no longer hard-fails on unread files). File-op
            # tracking and result truncation below stay on.
            enforce_read_before_edit=False,
            truncate_results=True,
            include_system_prompt=True,
        ),
        # Break stuck tool-call loops: if the model fires the same tool with the
        # same args and gets the same result 3× in a row (e.g. a grep that keeps
        # returning "no matches"), short-circuit the next identical call with a
        # nudge to change approach instead of letting it spin indefinitely.
        LoopGuardMiddleware(threshold=3),
        # Rubric self-evaluation (deepagents). Dormant unless a `rubric` is passed
        # on the invoke state — set via `/goal rubric <criteria>`. When present, a
        # grader sub-agent scores the finished run against the criteria and, on a
        # miss, injects per-criterion feedback and lets the agent revise (up to
        # max_iterations). Uses Nova's own model so there's no provider/key
        # assumption; default grader prompt. No rubric ⇒ zero added cost.
        _rubric_middleware,
        # Context editing — clear older tool call outputs when token limits
        # are reached, preserving only the most recent results. This is a
        # lightweight, deterministic alternative to LLM-based compaction.
        # Placed after FileTracker (which needs full history) and before
        # ShellMiddleware/AgentMemoryMiddleware so it operates on the final
        # message list that will be sent to the model.
        #
        # The trigger is WINDOW-RELATIVE: the library takes an absolute token
        # count, so the fraction is resolved here against the bound model's
        # window. A fixed count was window-independent — it never fired on the
        # ~40K Ollama windows in MODEL_CONTEXT_WINDOWS (leaving those models
        # with no tool-result reducer at all) while clearing needlessly early
        # on 200K models. Kept below AUTO_COMPACT_THRESHOLD so the cheap
        # reducer always gets its chance before whole-history compaction.
        ContextEditingMiddleware(
            edits=[
                _tool_result_clearing(
                    context_window,
                    cleared_dir(agent_dir) if agent_dir else None,
                )
            ]
        ),
        ShellMiddleware(
            workspace_root=str(workspace_root),
            env=dict(os.environ),
            backend=composite_backend,  # Route through CompositeBackend for /skills/ etc.
            # When in a sandbox, show the in-sandbox working dir (e.g. /workspace)
            # in the tool description instead of the host path.
            sandbox_working_dir=(get_default_working_dir(sandbox_type) if sandbox_type else None),
            # Pattern A: confine local shell execution to the workspace via an OS
            # kernel sandbox. Only meaningful in local mode (no sandbox backend).
            exec_sandbox=exec_sandbox and sandbox is None,
        ),
        AgentMemoryMiddleware(
            settings=settings,
            assistant_id=assistant_id,
            # Always load project memory (NOVA.md/CLAUDE.md) into <project_memory>,
            # even on resume. It's standing project config that must be present in
            # the system prompt EVERY turn (and hot-reloaded). The continuation
            # prompt no longer carries NOVA.md, so there's nothing to duplicate —
            # and unlike a seeded message, the system-prompt copy survives
            # summarization/compaction.
            skip_project_memory=False,
            backend=composite_backend,  # Route through CompositeBackend for /memories/ etc.
            # Sizes the per-block injection budget: four memory blocks at a flat
            # 12k chars each is 12% of a 200K window but a third of a 40K one.
            context_window=context_window,
        ),
        # Last, so its todo recitation is appended to the FINAL system message
        # (AgentMemoryMiddleware rebuilds it) and sits after the cache breakpoint.
        TaskDisciplineMiddleware(),
    ]

    # deepagents 0.7 dropped its built-in todo list (0.6 installed one inside
    # create_deep_agent). Without it `write_todos` silently disappears: the
    # workflow prompt, the todo recitation and the done gate all assume it.
    # Placed before AgentMemoryMiddleware so its prompt joins the cached base.
    import deepagents.graph as _deepagents_graph

    if not hasattr(_deepagents_graph, "TodoListMiddleware"):
        from langchain.agents.middleware import TodoListMiddleware

        agent_middleware.insert(
            agent_middleware.index(agent_middleware[-2]),  # before AgentMemoryMiddleware
            TodoListMiddleware(
                system_prompt="## `write_todos`\n\nRules for the todo list are in <todo_management>."
            ),
        )

    return agent_middleware


#: MCP servers each subagent may use, keyed by subagent name. A subagent absent
#: from this map gets NO MCP tools. Keep this list short and deliberate: every
#: tool schema granted here is serialized into the ``task`` tool schema on every
#: turn, and a subagent that cannot use a tool will still try to call it.
MCP_TOOLS_BY_SUBAGENT: dict[str, tuple[str, ...]] = {
    # Its whole purpose is driving a real browser; without playwright it can only
    # fetch HTML and had to disclaim the work in its own description.
    "browser-automation-agent": ("playwright",),
    # Semantic code navigation (LSP-backed symbol search) for repo exploration.
    "code-explorer": ("serena",),
}


def _grant_mcp_tools(subagents: list, mcp_tools: list[BaseTool]) -> None:
    """Append the opted-in MCP tools to each subagent's ``tools`` list, in place.

    MCP tool names are prefixed with the server name (``serena_read_file``,
    ``playwright_browser_click``), so a server is matched by prefix. Subagents
    that are not in :data:`MCP_TOOLS_BY_SUBAGENT`, and specs that are compiled
    runnables or remote agents (no ``tools`` key), are left untouched.

    Idempotent by construction. The spec dicts handed back by
    ``retrieve_core_subagents``/``build_named_subagents`` are **cached and reused**
    across every ``create_agent_with_config`` call (the session agent, then
    /init's dedicated agent, …), so appending blindly accumulated a duplicate
    copy of every granted tool on the second build — the same failure mode that
    made /init fall back to the shared agent (see :func:`_harden_subagent_specs`).
    Tools already present are therefore skipped, and the list is rebuilt rather
    than mutated so the cached spec is never left holding a longer list.
    """
    for spec in subagents:
        if not isinstance(spec, dict):
            continue
        servers = MCP_TOOLS_BY_SUBAGENT.get(spec.get("name", "")) or ()
        chosen = set(spec.get("_nova_tool_names") or ())  # picked in /agents
        if not servers and not chosen:
            continue
        existing = list(spec.get("tools") or [])
        present = {_tool_name(t) for t in existing}
        granted = [
            t
            for t in mcp_tools
            if (name := _tool_name(t))
            and name not in present
            and (name in chosen or any(name.startswith(f"{server}_") for server in servers))
        ]
        if not granted:
            continue
        spec["tools"] = [*existing, *granted]


def _build_subagent_roster(
    *,
    assistant_id: str,
    tools: list[BaseTool],
    plugin_specs: list,
    skill_sources: list[str],
    mcp_tools: list[BaseTool] | None = None,
    main_model_supports_images: bool = False,
) -> list:
    """Assemble the final subagent roster for ``create_deep_agent``.

    Core + named + plugin + general-purpose subagents, hardened via
    :func:`_harden_subagent_specs`, then the async subagents (which own their
    own config and are left untouched).

    ``mcp_tools`` are the tools discovered by ``MCPMiddleware``. They are NOT
    added to every subagent: each opted-in subagent receives only the servers
    named for it in ``MCP_TOOLS_BY_SUBAGENT``, because every tool schema handed
    to a subagent is serialized into the ``task`` tool schema on every turn.
    """
    Nova_SubAgent: list[SubAgent] = []

    # Load pre-defined default and user defined subagents
    Nova_SubAgent.extend(retrieve_core_subagents(tools=tools))  # type: ignore
    Nova_SubAgent.extend(build_named_subagents(assistant_id=assistant_id, tools=tools))  # type: ignore

    # Opt-in MCP tools: give a subagent the MCP servers it is actually built to
    # use. Without this, MCP tools are reachable only from the main agent (they
    # attach via MCPMiddleware.tools, while subagent specs are built from the
    # plain tool list) — which is why browser-automation-agent used to accept
    # browser tasks it had no way to perform.
    if mcp_tools:
        _grant_mcp_tools(Nova_SubAgent, mcp_tools)

    # Plugin subagents — delegate agents contributed by enabled plugins. Added
    # after the built-ins so they go through the same _harden_subagent_specs pass
    # below (retry + no nested HITL) and can be dispatched via the `task` tool.
    if plugin_specs:
        try:
            from novacode_cli.plugins.loader import merge_plugin_subagents

            merge_plugin_subagents(Nova_SubAgent, plugin_specs)  # type: ignore
        except Exception:  # noqa: BLE001
            __import__("logging").getLogger("nova.plugins").exception(
                "Failed to merge plugin subagents"
            )

    # Own the general-purpose subagent instead of letting create_deep_agent
    # auto-inject it. The auto-injected one inherits the main `interrupt_on` and
    # gets NO retry middleware, so it (a) can crash the turn on a nested HITL
    # interrupt and (b) dies on a transient provider 5xx. /init delegates its
    # semantic-extraction chunks to general-purpose, so this is exactly the
    # subagent that was failing. Providing our own spec (the documented override)
    # routes it through _harden_subagent_specs below — retry + no nested HITL —
    # while deepagents still builds its base middleware stack. The stock
    # GENERAL_PURPOSE_SUBAGENT system prompt is used (we forgo the harness-profile
    # prompt overlay, which is an acceptable trade for resilience).
    from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

    if not any(isinstance(s, dict) and s.get("name") == "general-purpose" for s in Nova_SubAgent):
        Nova_SubAgent.append({**GENERAL_PURPOSE_SUBAGENT, "tools": tools})  # type: ignore

    # Subagents from installed Claude-compatible plugins (agents/*.md).
    from novacode_cli.plugins.claude_plugins import plugin_agent_specs

    _existing = {s.get("name") for s in Nova_SubAgent if isinstance(s, dict)}
    Nova_SubAgent.extend(s for s in plugin_agent_specs() if s["name"] not in _existing)

    # Load async subagents (run on remote LangGraph servers in background)
    async_subagents = retrieve_async_subagents()

    # Subagents run UNATTENDED — the main agent is the sole HITL boundary.
    #
    # Why: create_deep_agent propagates the main `interrupt_on` to every
    # declarative subagent (deepagents.graph). But a subagent is invoked via
    # `subagent.ainvoke()` inside the `task` tool, so a HITL interrupt raised
    # INSIDE a subagent surfaces as a GraphInterrupt EXCEPTION bubbling out of
    # the parent agent's stream — it never appears as a top-level
    # `__interrupt__` event, so the approve/auto-approve path in run_agent_stream
    # never sees it and the whole turn crashes (this is what broke /init's
    # semantic-extraction and NOVA.md-authoring subagents, and would break any
    # /research subagent that writes a file). Nested HITL is unresolvable in this
    # architecture, so we explicitly clear `interrupt_on` on each declarative
    # subagent. The main agent still gates its own destructive tools; subagents
    # it dispatches do not independently prompt. Compiled/remote subagents own
    # their own approval config and are left untouched.
    #
    # The same pass also gives each subagent a ModelRetryMiddleware (which they
    # otherwise lack entirely) so a transient provider 5xx/429 no longer kills a
    # subagent mid-run — see _harden_subagent_specs. Returns fresh copies (never
    # mutates the cached specs, which would accumulate middleware across builds).
    Nova_SubAgent = _harden_subagent_specs(
        Nova_SubAgent, skill_sources, main_model_supports_images
    )

    return Nova_SubAgent + async_subagents


def create_agent_with_config(
    model: str | BaseChatModel,
    assistant_id: str,
    tools: list[BaseTool],
    *,
    sandbox: SandboxBackendProtocol | None = None,
    sandbox_type: str | None = None,
    system_prompt: str | None = None,
    auto_approve: bool = False,
    store: BaseStore | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    steering_instructions: list | None = None,
    exec_sandbox: bool = False,
    session_id: str | None = None,
    workspace_root: str | None = None,
    extra_middleware: list | None = None,
) -> tuple[Pregel, CompositeBackend]:
    """Create and configure an agent with the specified model and tools.

    ``workspace_root`` overrides the default (``settings.get_workspace_root()``) —
    used by Cowork to root the agent at a granted folder. ``extra_middleware`` is
    appended to the stack (Cowork injects its WorkspacePolicy broker there).

    Args:
        model: LLM model to use
        assistant_id: Agent identifier for memory storage
        tools: Additional tools to provide to agent
        sandbox: Optional sandbox backend for remote execution (e.g., ModalBackend).
                 If None, uses local filesystem + shell.
        sandbox_type: Type of sandbox provider ("modal", "runloop", "daytona")
        exec_sandbox: When True (Pattern A, local mode), confine locally-executed
                 shell commands to the workspace via an OS kernel sandbox
                 (bwrap/sandbox-exec). No effect when a sandbox backend is used.
        store: Optional durable store (BaseStore). If None, the caller is
               expected to pass the shared store from get_shared_store() so
               subagents can also access it.

    Returns:
        2-tuple of (graph, backend)
    """
    # Lazy import for tracing (speeds up startup)
    from novacode_cli.tracking.tracing import is_tracing_enabled, get_tracing_config

    # Agent-to-agent mailbox on the orchestrator too: it can message its
    # subagents by name and read replies. Copy (never mutate the caller's list);
    # subagents that inherit the main tool list (tools=None) get these for free.
    from novacode_cli.agents.agent_mailbox import read_agent_messages, send_agent_message

    _have_tool = {getattr(t, "name", None) for t in tools}
    _mb_extra = [t for t in (send_agent_message, read_agent_messages) if t.name not in _have_tool]
    if _mb_extra:
        tools = list(tools) + _mb_extra

    tracing_enabled = False

    if is_tracing_enabled():
        tracing_enabled = True
        tracing_config = get_tracing_config()
        # console.print(
        #     f"[dim]LangSmith tracing enabled: {tracing_config.project_name}[/dim]"
        # )
    else:
        # Try to auto-configure from environment
        from novacode_cli.tracking.tracing import auto_configure

        config_result = auto_configure()
        if config_result.is_configured():
            tracing_enabled = True
            # console.print(
            #     f"[dim]LangSmith tracing enabled: {config_result.project_name}[/dim]"
            # )

    # Wrap model for OpenAI tracing if enabled and model is a ChatOpenAI instance
    wrapped_model = model
    if tracing_enabled and hasattr(model, "_model"):  # Check if it's a LangChain model
        try:
            from langchain_openai import ChatOpenAI

            if isinstance(model, ChatOpenAI):
                from novacode_cli.tracking.tracing import (
                    wrap_openai_client as _wrap_openai,
                )

                wrapped_model = _wrap_openai(model)
        except ImportError:
            pass

    # Skill sources + the directories backing them (global ~/.nova/skills/,
    # ~/.claude/skills/, project-level, and installed-plugin skills).
    (
        skill_sources,
        skills_dir,
        claude_skills_dir,
        project_skills_dirs,
        _plugin_skills,
    ) = _build_skill_sources()

    # Determine workspace root for path containment (resolves to subdirectory if applicable)
    workspace_root = Path(workspace_root) if workspace_root else settings.get_workspace_root()

    # Build list of allowed directories for filesystem access
    # This includes the workspace root plus user directories like skills, memory, etc.
    allowed_prefixes = [str(workspace_root)]
    if settings.project_root and settings.project_root != workspace_root:
        allowed_prefixes.append(str(settings.project_root))

    # Add user skills directory (~/.nova/skills/)
    if skills_dir:
        allowed_prefixes.append(str(skills_dir))

    # Add global Claude Code skills directory (~/.claude/skills/)
    if claude_skills_dir.exists():
        allowed_prefixes.append(str(claude_skills_dir))

    # Add project skills directories
    for skills_path in project_skills_dirs:
        allowed_prefixes.append(str(skills_path))

    # Add user agent directory (~/.nova/<agent>/) for memory files
    agent_dir = settings.get_agent_dir(assistant_id)
    if agent_dir:
        allowed_prefixes.append(str(agent_dir))
        # Auto-create simple memory structure on first use so the agent has
        # a memory file to read/write without needing the
        # create_memory_structure tool (which is no longer registered in the
        # default toolset).
        _agent_md = agent_dir / "agent.md"
        if not _agent_md.exists():
            agent_dir.mkdir(parents=True, exist_ok=True)
            _agent_md.write_text(
                """# Agent Memory

This file stores your preferences and context that persist across sessions.

## Communication Style
- [Your preferred communication style]

## Coding Preferences
- [Your coding preferences]

## Project Context
- [Project-specific notes]

## Workflows
- [Common workflows you use]
""",
                encoding="utf-8",
            )

        # Ensure the semantic-tier scaffolding (memories/ dir) exists, and
        # one-time-migrate any legacy USER.md/MEMORY.md into the injected
        # surface (agent.md + memories/). migrate_legacy_tiers renames consumed
        # files to *.migrated.bak, so a second run is a no-op.
        try:
            from novacode_cli.hermes.memory_tiers import (
                ensure_memory_tiers,
                migrate_legacy_tiers,
            )

            ensure_memory_tiers(agent_dir)
            migrate_legacy_tiers(agent_dir)
        except Exception:  # noqa: BLE001
            console.print("[dim]⚠ Failed to ensure Nova memory tiers[/dim]")

    # Default backend (local shell vs remote sandbox) + CompositeBackend with
    # all virtual-path routes (/skills/, /memories/, /store/, /.nova/plans/, …).
    composite_backend, _default_backend = _build_composite_backend(
        sandbox=sandbox,
        sandbox_type=sandbox_type,
        workspace_root=workspace_root,
        skills_dir=skills_dir,
        claude_skills_dir=claude_skills_dir,
        project_skills_dirs=project_skills_dirs,
        plugin_skills=_plugin_skills,
        agent_dir=agent_dir,
        store=store,
        assistant_id=assistant_id,
        session_id=session_id,
    )

    # Check whether MCP servers are configured before instantiating the middleware.
    # MCPMiddleware calls list_servers() (a JSON file read) on every model turn, so
    # skipping it entirely when there are no servers saves ~4 file reads per call.
    _has_mcp = False
    try:
        from novacode_cli.mcp.config import MCPConfig as _MCPConfig

        _has_mcp = bool(_MCPConfig().load())
    except Exception:
        pass

    _model_name = (
        model
        if isinstance(model, str)
        else getattr(model, "model_name", getattr(model, "model", "unknown"))
    )

    # Whether the main model can see images. When it can, images are passed
    # straight through instead of being captioned by the auxiliary vision model
    # (see _resolve_main_model_multimodal). Computed once and shared by the main
    # stack and every subagent — subagents inherit the main model.
    from novacode_cli.config.model_capabilities import note_bound_model

    note_bound_model(model)
    _main_model_supports_images = _resolve_main_model_multimodal(model)
    if _main_model_supports_images:
        # Not redundant with the line above: that decides what NOVA does, this
        # tells DEEPAGENTS not to strip the image on the way to the model.
        _declare_image_support(model)

    # Resolve the bound model's effective window exactly ONCE per build and pass
    # it to both consumers: the window-relative context-editing trigger below and
    # deepagents' summarization trigger (via _seed_summarization_profile), so both
    # reducers measure the same budget the ctx% indicator displays. Resolving it
    # twice would be a real cost — for a local Ollama model the probe shells out
    # to an uncached `ollama ps` (10s timeout) on every build.
    try:
        from novacode_cli.context import ContextManager

        _context_window = ContextManager(_model_name).window_size()
    except Exception:  # noqa: BLE001 — an unknown window falls back to fixed triggers
        _context_window = 0

    # Core middleware stack (retry → vision → learning → security → bootstrap →
    # steering → file tracker → loop guard → rubric → context editing → shell →
    # agent memory). Ordering is load-bearing — see _build_middleware_stack.
    agent_middleware = _build_middleware_stack(
        model=model,
        assistant_id=assistant_id,
        store=store,
        skills_dir=skills_dir,
        agent_dir=agent_dir,
        workspace_root=workspace_root,
        steering_instructions=steering_instructions,
        composite_backend=composite_backend,
        sandbox=sandbox,
        sandbox_type=sandbox_type,
        exec_sandbox=exec_sandbox,
        context_window=_context_window,
        main_model_supports_images=_main_model_supports_images,
    )

    # Plugin middleware injection — discover and inject user-enabled plugins
    # from installed pip packages that register "nova.plugins" entry points.
    # Plugins sit after the built-in middleware assembly but before the optional
    # MCP middleware so slot-based positioning remains predictable regardless of
    # whether MCP is configured on this machine.
    _plugin_specs: list = []
    try:
        from novacode_cli.plugins.loader import (
            discover_enabled_plugins,
            merge_plugin_middleware,
            merge_plugin_tools,
        )

        _plugin_specs = discover_enabled_plugins()
        if _plugin_specs:
            merge_plugin_middleware(agent_middleware, _plugin_specs)
            merge_plugin_tools(tools, _plugin_specs)
            # Surface lifecycle hooks if any plugins registered them
            for _pkg_name, _spec in _plugin_specs:
                _hooks = _spec.get("hooks") or {}
                _before = _hooks.get("before_agent_setup")
                if _before:
                    try:
                        import asyncio

                        asyncio.create_task(_before())
                    except Exception:
                        logger = __import__("logging").getLogger("nova.plugins")
                        logger.exception("Plugin '%s' before_agent_setup hook failed", _pkg_name)
    except Exception:
        pass

    # MCP middleware: only add when servers are actually configured.
    # Insert after GraphContext (index 3 now that ModelRetryMiddleware leads the
    # stack) so MCP tools keep their original position relative to the others.
    mcp_tools: list[BaseTool] = []
    if _has_mcp:
        from novacode_cli.mcp import get_shared_mcp_middleware

        mcp_middleware = get_shared_mcp_middleware()
        # Eagerly discover MCP tools BEFORE building the agent graph.
        # create_deep_agent collects each middleware's ``.tools`` at construction
        # time (langchain factory: ``[t for m in middleware for t in m.tools]``),
        # so lazy discovery would leave ``mcp_middleware.tools`` empty and the
        # MCP tools (serena, playwright, …) would never be registered or callable.
        if not mcp_middleware._tools_discovered:
            try:
                boot_status("Nova is launching…")
                mcp_middleware._discover_tools_sync()
            except Exception as exc:  # noqa: BLE001
                boot_status(f"mcp: some servers are still at large ({type(exc).__name__})", "warn")
        agent_middleware.insert(3, mcp_middleware)
        mcp_tools = list(mcp_middleware.tools)

    # NOTE: automatic context-window summarization is provided by
    # create_deep_agent's built-in SummarizationMiddleware (part of its tail
    # stack) — do NOT add another here or agent creation fails with
    # "duplicate middleware instances".

    # Final subagent roster: core + named + plugin + general-purpose + async,
    # hardened for unattended dispatch — see _build_subagent_roster.
    #
    # ``mcp_tools`` is passed separately from ``tools``: subagent specs are built
    # from the plain tool list, so MCP tools (which attach via
    # ``MCPMiddleware.tools``) would otherwise be unreachable from any subagent.
    # Only the subagents that opt in via ``MCP_TOOLS_BY_SUBAGENT`` receive them.
    subagents = _build_subagent_roster(
        assistant_id=assistant_id,
        tools=tools,
        mcp_tools=mcp_tools,
        plugin_specs=_plugin_specs,
        skill_sources=skill_sources,
        main_model_supports_images=_main_model_supports_images,
    )

    # Get the system prompt (sandbox-aware and with skills)
    if system_prompt is None:
        system_prompt = get_system_prompt(
            assistant_id=assistant_id, sandbox_type=sandbox_type, exec_sandbox=exec_sandbox
        )

    if auto_approve:
        # No interrupts - all tools run automatically
        interrupt_on = {}
    else:
        # Full HITL for destructive operations
        interrupt_on = get_interrupt_configs()

    # Make deepagents' built-in SummarizationMiddleware actually fire on OUR
    # context budget (see _seed_summarization_profile). Reuse the window already
    # resolved above for the context-editing trigger — for a local Ollama model a
    # second resolve would re-run the uncached `ollama ps` probe.
    _seed_summarization_profile(wrapped_model, _model_name, window=_context_window)

    # Pass named_subagents directly to create_deep_agent
    # It will create the SubAgentMiddleware internally
    # Use provided checkpointer or fallback to InMemorySaver
    final_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()

    # Refresh the injected skills list when the skill dirs change, so a skill
    # created mid-session (skill_manage / Hermes review) is usable this session
    # instead of only after a restart. This replaces create_deep_agent's default
    # SkillsMiddleware (see skills=None below).
    skill_watch_dirs = [skills_dir]
    if claude_skills_dir.exists():
        skill_watch_dirs.append(claude_skills_dir)
    skill_watch_dirs.extend(project_skills_dirs)
    agent_middleware.append(
        RefreshingSkillsMiddleware(
            backend=composite_backend,
            sources=skill_sources,
            watch_dirs=skill_watch_dirs,
            listing_chars=listing_budget(_context_window),
        )
    )
    # Skill curation — clamp the loaded skills to the user-enabled set. MUST be
    # appended AFTER RefreshingSkillsMiddleware so its before_agent node runs
    # after the loader populates skills_metadata (toggled off skills then vanish
    # from the system-prompt list and the agent's reach). See /skills toggles.
    agent_middleware.append(SkillCurationMiddleware())

    # Dynamic subagents — a QuickJS `eval` tool whose scripts can dispatch
    # subagents via a top-level `task()` host function (fan-out, verify, loop
    # patterns; triggered by "workflow" prompts). It bridges into the same
    # `task` tool deepagents registers, so dispatches reuse the hardened
    # subagent specs above. Host `task()` awaits pause the JS timeout clock,
    # so long subagent runs don't trip the 5s eval budget. Import-guarded so
    # installs that predate the langchain-quickjs dependency still boot.
    # ponytail: PTC off (tools.* not exposed to scripts); enable with an
    # allowlist if scripts need glob/grep discovery.
    try:
        import warnings

        from langchain_quickjs import CodeInterpreterMiddleware

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # beta-API warning would print every boot
            agent_middleware.append(CodeInterpreterMiddleware())
    except ImportError:
        pass

    # Repair unanswered tool calls before EVERY model call. deepagents' own
    # PatchToolCalls only runs once per invocation, so a call left unanswered
    # mid-turn reached the next model call as-is — which OpenAI-strict
    # endpoints reject outright ("An assistant message with 'tool_calls' must
    # be followed by tool messages..."). See agents/tool_call_repair.py.
    from novacode_cli.agents.tool_call_repair import RepairToolCallsEachStep

    agent_middleware.append(RepairToolCallsEachStep())

    # Bind only core / frequent / already-loaded tools; `tool_search` loads the
    # rest (MCP servers alone are ~75 schemas). Plugin subagents leave the
    # `task` description the same way. Innermost, so it filters the final list.
    from novacode_cli.agents.tool_search import ToolSearchMiddleware
    from novacode_cli.plugins.claude_plugins import plugin_agent_specs

    _plugin_agents = {s["name"] for s in plugin_agent_specs()}
    agent_middleware.append(
        ToolSearchMiddleware(
            deferred_subagents={
                s["name"]: s.get("description", "")
                for s in subagents
                if isinstance(s, dict) and s.get("name") in _plugin_agents
            }
        )
    )

    # Caller-injected middleware (e.g. Cowork's WorkspacePolicy broker) goes last
    # so it wraps tool calls closest to execution — a denied call never runs.
    if extra_middleware:
        agent_middleware.extend(extra_middleware)

    agent = create_deep_agent(
        name=assistant_id,
        model=wrapped_model,
        skills=None,  # our RefreshingSkillsMiddleware (in agent_middleware) handles skills
        system_prompt=system_prompt,
        tools=tools,
        checkpointer=final_checkpointer,
        backend=composite_backend,
        middleware=agent_middleware,
        store=store,
        interrupt_on=interrupt_on,  # type: ignore
        subagents=subagents,  # type: ignore
    ).with_config(
        config  # type: ignore
    )

    # Plugin after_agent_setup hooks — call any registered lifecycle hooks
    # now that the agent graph is fully built.
    try:
        from novacode_cli.plugins.loader import discover_enabled_plugins

        for _pkg_name, _spec in discover_enabled_plugins():
            _hooks = _spec.get("hooks") or {}
            _after = _hooks.get("after_agent_setup")
            if _after:
                try:
                    import asyncio

                    asyncio.create_task(_after(agent))  # type: ignore
                except Exception:
                    _log = __import__("logging").getLogger("nova.plugins")
                    _log.exception("Plugin '%s' after_agent_setup hook failed", _pkg_name)
    except Exception:
        pass

    return agent, composite_backend
