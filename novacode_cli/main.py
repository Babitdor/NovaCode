"""Main entry point and session bootstrap for NovaCode.

This module provides the primary CLI interface for nova-Code CLI, including:
- Command-line argument parsing and validation
- Session setup (agent, tools, middleware, backends) and hand-off to the TUI
- Session management (save, restore, auto-save)
- Command handling for special CLI commands (/help, /tokens, etc.)
- Integration with sandbox backends and agent configuration
- Auto-save functionality for session persistence

The interactive UI is the Textual TUI (``novacode_cli.tui``); this module only
builds the session and launches it. The non-interactive paths are headless mode
(``-p``), the parallel-session worker, and the remote bridges.

This module's job ends at handing a configured session to the TUI:
1. Agent initialization with configuration and backends
2. Session state management and persistence
3. Launching the TUI (which owns input, streaming, rendering and approvals)

Key Functions:
- parse_args(): Parse command-line arguments
- cli_main(): Main entry point for the CLI
- main(): Async entry point that sets up the agent and session
- _run_agent_session(): Build the agent/session, then hand off to the TUI
"""

# Suppress transformer warnings before any imports that might trigger them
import os
import warnings

# Suppress "None of PyTorch, TensorFlow >= 2.0, or Flax have been found" warning
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

# Silence HuggingFace Hub symlink warning on Windows (Semble/model2vec download)
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Suppress token sequence length warnings from transformers/tiktoken
warnings.filterwarnings(
    "ignore",
    message="Token indices sequence length is longer than",
)
warnings.filterwarnings(
    "ignore",
    message="None of PyTorch, TensorFlow",
)
warnings.filterwarnings(
    "ignore",
    message="Using fallback GPT-2 tokenizer for token counting",
)
# Instant boot feedback — printed BEFORE the heavy imports below (langchain,
# deepagents, anthropic ≈ several seconds cold) so the terminal isn't blank
# while Python loads. stderr + isatty keeps piped/scripted output clean.
import sys as _sys

if _sys.stderr.isatty():
    _sys.stderr.write("\x1b[2m· nova: loading…\x1b[0m\n")
    _sys.stderr.flush()

# Suppress deepagents files_update deprecation warning (handled internally by deepagents)
from langchain_core._api.deprecation import LangChainDeprecationWarning

warnings.filterwarnings(
    "ignore",
    message=".*files_update.*",
    category=LangChainDeprecationWarning,
)

import argparse
import asyncio
import io
import logging
import sys
from pathlib import Path

# Fix Windows console encoding to handle Unicode characters
# This must be done before any output is written.
# Skip under pytest: replacing sys.stdout/stderr here would detach pytest's
# output capture (its buffers get closed at session end -> "I/O operation on
# closed file"). The real CLI never runs under pytest, so this only guards tests.
if sys.platform == "win32" and "pytest" not in sys.modules:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Configure logging to a file for troubleshooting
# Logs are saved to ~/.nova/logs/nova.log
log_dir = Path.home() / ".nova" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "nova.log"

# Configure file handler for warnings
file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
file_handler.setLevel(logging.WARNING)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(file_handler)

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver as _AsyncSqliteSaver  # type: ignore

    _SQLITE_CHECKPOINTER_AVAILABLE = True
except ImportError:
    _SQLITE_CHECKPOINTER_AVAILABLE = False

# Apply safety patches for backends that don't handle all content block types.
# The Ollama content-block patch is NOT applied here: it would import
# langchain_ollama (~1s) for every user. It's applied at ChatOllama
# construction time in model_create.py / model_manager.py instead.
# The filesystem/write_file patches are applied in _run_agent_session (not at
# module import) so they don't pull deepagents (~4s) at CLI startup.

from novacode_cli.commands.commands import (
    execute_skills_command,
)
from novacode_cli.config.config import (
    COLORS,
    HOME_DIR,
    boot_status,
    console,
    format_version_banner,
    settings,
)
from novacode_cli.config.model_create import create_model, create_model_for_session
from novacode_cli.utils.model_info import get_current_provider
from novacode_cli.hooks import HookEvent, dispatch_hook_fire_and_forget
from novacode_cli.ui.ui_elements import show_help
from novacode_cli.tracking.tracing import auto_configure as _auto_configure_tracing

# Module logger for background services (cron scheduler, remote processor).
_proc_logger = logging.getLogger("novacode_cli.remote")

# Initialize LangSmith tracing from environment variables (no-op when not configured)
_auto_configure_tracing()
from novacode_cli.mcp.commands import execute_mcp_command, setup_mcp_parser
from novacode_cli.migrate import check_migration_status, migrate_agents
from novacode_cli.path_approval import PathApprovalManager, check_path_approval
from novacode_cli.skills.skill_creation import setup_skills_parser
from novacode_cli.states.Session import SessionState
from novacode_cli.tools import (
    code_search,
    docs_search,
    duckduckgo_search,
    fetch_url,
    find_related_code,
    forget,
    github_trending,
    hacker_news,
    linkedin_jobs,
    list_memories,
    package_info,
    read_memory,
    recall,
    reddit_posts,
    oracle,
    remember,
    skill_manage,
    speak,
    think,
    web_search,
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
# Vixie desktop pet integration
from novacode_cli.vixie.server import start_vixie_server, stop_vixie_server

from novacode_cli.process_manager import ProcessManager


def check_cli_dependencies() -> None:
    """Check if CLI optional dependencies are installed."""
    missing = []

    try:
        import rich
    except ImportError:
        missing.append("rich")

    try:
        import requests
    except ImportError:
        missing.append("requests")

    try:
        import dotenv
    except ImportError:
        missing.append("python-dotenv")

    try:
        import tavily
    except ImportError:
        missing.append("tavily-python")

    if missing:
        print("\n❌ Missing required CLI dependencies!")
        print("\nThe following packages are required to use the deepagents CLI:")
        for pkg in missing:
            print(f"  - {pkg}")
        print("\nPlease install them with:")
        print("  uv add 'deepagents[cli]'")
        sys.exit(1)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="DeepAgents - AI Coding Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Init command - interactive configuration setup
    init_parser = subparsers.add_parser("init", help="Initialize project or global configuration")
    init_parser.add_argument(
        "--scope",
        choices=["project", "global"],
        help="Create project-specific or global configuration",
    )
    init_parser.add_argument(
        "--style",
        choices=["deepagents", "claude"],
        help="Use .nova/ or .claude/ directory structure",
    )
    init_parser.add_argument(
        "--reset",
        action="store_true",
        help="Re-run onboarding wizard to reset configuration",
    )

    # List command
    subparsers.add_parser("list", help="List all available agents")

    # Help command
    subparsers.add_parser("help", help="Show help information")

    # Reset command
    reset_parser = subparsers.add_parser("reset", help="Reset an agent")
    reset_parser.add_argument("--agent", required=True, help="Name of agent to reset")
    reset_parser.add_argument(
        "--target", dest="source_agent", help="Copy prompt from another agent"
    )

    # Skills command - setup delegated to skills module
    setup_skills_parser(subparsers)

    # MCP command - setup delegated to mcp module
    setup_mcp_parser(subparsers)

    # Config command - view/edit configuration
    config_parser = subparsers.add_parser("config", help="View or edit configuration (non-secret)")
    config_parser.add_argument(
        "config_command",
        nargs="?",
        choices=["show", "set", "get"],
        default="show",
        help="Config operation to perform",
    )
    config_parser.add_argument(
        "key",
        nargs="?",
        help="Configuration key to get/set",
    )
    config_parser.add_argument(
        "value",
        nargs="?",
        help="Value to set (for 'set' command)",
    )

    # Secrets command - manage API keys
    secrets_parser = subparsers.add_parser("secrets", help="Manage API keys securely")
    secrets_parser.add_argument(
        "secrets_command",
        choices=["set", "list", "delete"],
        help="Secrets operation to perform",
    )
    secrets_parser.add_argument(
        "key",
        nargs="?",
        help="API key name (e.g., 'openai_api_key')",
    )

    # Doctor command - validate setup
    subparsers.add_parser("doctor", help="Validate configuration and connections")

    # Paths command - manage approved paths
    paths_parser = subparsers.add_parser(
        "paths",
        help="Manage approved file system paths",
    )
    paths_subparsers = paths_parser.add_subparsers(dest="paths_command", help="Paths command")

    # paths list
    paths_subparsers.add_parser(
        "list",
        help="List all approved paths",
    )

    # paths revoke
    revoke_parser = paths_subparsers.add_parser(
        "revoke",
        help="Revoke approval for a path",
    )
    revoke_parser.add_argument(
        "path",
        help="Path to revoke (absolute path)",
    )

    # paths clear
    paths_subparsers.add_parser(
        "clear",
        help="Clear all approved paths",
    )

    # Migrate command - migrate from old to new directory structure
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Migrate from old directory structure to new Claude Code-compatible structure",
    )
    migrate_parser.add_argument(
        "--check",
        action="store_true",
        help="Check migration status without performing migration",
    )

    # Default interactive mode
    parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for separate memory stores (default: nova-agent).",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Auto-approve tool usage without prompting (disables human-in-the-loop)",
    )
    parser.add_argument(
        "--sandbox",
        choices=["none", "os", "modal", "daytona", "runloop", "docker", "langsmith"],
        default=None,
        help="Sandbox for code execution. Default: 'os' on Linux/macOS (files on the "
        "host, shell confined to the workspace via an OS sandbox); host execution + "
        "approvals on Windows. 'docker' is an opt-in, Windows-only container. "
        "'langsmith' uses LangSmith Sandboxes (hardware-virtualized microVMs). Use "
        "--no-sandbox for unconfined local execution.",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Run shell commands unconfined on the host (disables the OS/Docker sandbox)",
    )
    parser.add_argument(
        "--sandbox-id",
        help="Existing sandbox ID to reuse (skips creation and cleanup)",
    )
    parser.add_argument(
        "--sandbox-setup",
        help="Path to setup script to run in sandbox after creation",
    )
    parser.add_argument(
        "--sandbox-vcpus",
        type=int,
        default=None,
        help="Number of virtual CPUs for the sandbox (LangSmith only)",
    )
    parser.add_argument(
        "--sandbox-mem-bytes",
        type=int,
        default=None,
        help="Memory in bytes for the sandbox (LangSmith only). Example: 8589934592 for 8GB",
    )
    parser.add_argument(
        "--sandbox-fs-capacity-bytes",
        type=int,
        default=None,
        help="Filesystem capacity in bytes for the sandbox (LangSmith only)",
    )
    parser.add_argument(
        "--sandbox-snapshot",
        type=str,
        default=None,
        help="Snapshot name to boot the sandbox from (LangSmith only, mutually "
        "exclusive with --sandbox-snapshot-id)",
    )
    parser.add_argument(
        "--sandbox-snapshot-id",
        type=str,
        default=None,
        help="Snapshot ID to boot the sandbox from (LangSmith only, mutually "
        "exclusive with --sandbox-snapshot)",
    )
    parser.add_argument(
        "--ports",
        type=str,
        help="Port forwarding for Docker sandbox (format: 'PORT' or 'HOST_PORT:CONTAINER_PORT'). "
        "Multiple ports separated by comma. Example: '8080,3000:3000,5432:5432'",
    )
    parser.add_argument(
        "--no-splash",
        action="store_true",
        help="Disable the startup splash screen",
    )
    parser.add_argument(
        "--continue",
        "-c",
        dest="continue_session",
        nargs="?",
        const=True,
        default=False,
        help="Continue last session (optionally specify session ID)",
    )
    parser.add_argument(
        "--resume",
        "-r",
        action="store_true",
        help="Interactively select and resume a session",
    )
    # Headless (non-interactive) mode: run one prompt to completion and exit.
    parser.add_argument(
        "--print",
        "-p",
        dest="print_prompt",
        nargs="?",
        const=True,
        default=None,
        help="Run a single prompt non-interactively and exit. Pass the prompt as "
        "the value (nova -p \"...\"), or omit it to read the prompt from stdin "
        "(echo \"...\" | nova -p).",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help="Headless output format: 'text' (final answer only), 'json' (a single "
        "result object), or 'stream-json' (newline-delimited JSON events). "
        "Only used with --print.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Headless only: cap the number of agent turns (model steps). The run "
        "stops with a max-turns error if exceeded.",
    )
    parser.add_argument(
        "--deny-tools",
        action="store_true",
        help="Headless only: auto-reject tool approvals (fail-closed) instead of "
        "auto-approving. The agent runs read-only and reports what it could not do.",
    )
    # Internal: how a parent Nova TUI launches a parallel session. The child
    # speaks JSONL on stdio (see novacode_cli.sessions.worker) and is bound to
    # its own git worktree purely by the cwd it is spawned in. Not for humans.
    parser.add_argument("--session-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--version",
        action="version",
        version=format_version_banner(settings.version),
        help="Show the version number and exit",
    )
    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit")

    return parser.parse_args()


def _is_rate_limit_error(e: Exception) -> bool:
    """Check if an exception is a rate limit or API quota error."""
    msg = str(e).lower()
    if any(kw in msg for kw in ("429", "rate limit", "usage limit", "quota", "too many requests")):
        return True
    try:
        from openai import RateLimitError, APIStatusError

        if isinstance(e, (RateLimitError, APIStatusError)):
            return True
    except ImportError:
        pass
    try:
        from httpx import HTTPStatusError

        if isinstance(e, HTTPStatusError) and getattr(e.response, "status_code", 0) == 429:
            return True
    except ImportError:
        pass
    return False


def _is_api_error(e: Exception) -> bool:
    """Check if an exception is an API/model error that shouldn't crash the CLI."""
    try:
        from openai import APIStatusError, APIConnectionError

        if isinstance(e, (APIStatusError, APIConnectionError)):
            return True
    except ImportError:
        pass
    try:
        from httpx import HTTPStatusError

        if isinstance(e, HTTPStatusError) and getattr(e.response, "status_code", 0) >= 400:
            return True
    except ImportError:
        pass
    return False


async def _shutdown_background_services(session_state) -> None:
    """Best-effort teardown of background services on exit. Never raises.

    The TUI does its own session save on exit; this teardown makes sure the
    Vixie server, managed processes, remote bridges and
    background tasks don't dangle and throw during event-loop teardown (which
    can leave the terminal in a broken state).
    """

    async def _guard(coro, timeout: float = 4.0) -> None:
        """Await a teardown step but never let it hang or raise on quit."""
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except BaseException:  # noqa: BLE001 — incl. TimeoutError/CancelledError
            pass

    # Build best-effort stop coroutines and run them CONCURRENTLY, each bounded
    # by its own timeout — a single slow service (websocket, subprocess, bridge
    # socket) must not make /quit hang or feel unresponsive.
    steps: list = []

    steps.append(_guard(stop_vixie_server()))

    try:
        steps.append(_guard(ProcessManager.get_instance().stop_all()))
    except Exception:  # noqa: BLE001
        pass

    bridge_mgr = getattr(session_state, "_remote_bridge_manager", None)
    if bridge_mgr is not None:
        steps.append(_guard(bridge_mgr.stop_all()))

    # Trello task-board server (sync or async stop/close/shutdown).
    trello = getattr(session_state, "trello_server", None)
    if trello is not None:
        stop_fn = (
            getattr(trello, "stop", None)
            or getattr(trello, "close", None)
            or getattr(trello, "shutdown", None)
        )
        if callable(stop_fn):

            async def _stop_trello() -> None:
                res = stop_fn()
                if asyncio.iscoroutine(res):
                    await res

            steps.append(_guard(_stop_trello()))

    if steps:
        try:
            await asyncio.wait_for(asyncio.gather(*steps, return_exceptions=True), timeout=8.0)
        except BaseException:  # noqa: BLE001
            pass

    # Cancel any remote message-processor task (bounded).
    try:
        task = getattr(session_state, "_remote_processor_task", None)
        if task is not None and not task.done():
            task.cancel()
            await _guard(task, timeout=2.0)
    except Exception:  # noqa: BLE001, S110
        pass

    # Catch-all: cancel every other lingering task so asyncio.run's own shutdown
    # (which awaits all pending tasks) can't hang /quit on a slow or
    # uncancellable background coroutine — e.g. a Nova review / skill-creation
    # task, a fire-and-forget hook, or a remote reply still in flight.
    try:
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=3.0)
    except Exception:  # noqa: BLE001, S110
        pass


async def _run_agent_session(
    model,
    assistant_id: str,
    session_state,
    sandbox_backend=None,
    sandbox_type: str | None = None,
    setup_script_path: str | None = None,
    initial_messages: list | None = None,
    session_manager=None,
    store: BaseStore | None = None,
    checkpointer: InMemorySaver | None = None,
    restored_session_data: tuple | None = None,
    exec_sandbox: bool = False,
    model_provider: str | None = None,
) -> None:
    """Build the agent/session for this run, then launch the TUI.

    Not a REPL loop: it does the shared bootstrap (agent, tools, middleware,
    voice, remote bridges, cron) for both the local and sandbox branches, then
    ends by calling ``run_tui`` — the TUI is the only interactive UI.
    Extracted so the sandbox and local modes share one setup path.

    Args:
        model: LLM model to use
        assistant_id: Agent identifier for memory storage
        session_state: Session state with auto-approve settings
        sandbox_backend: Optional sandbox backend for remote execution
        sandbox_type: Type of sandbox being used
        exec_sandbox: When True (Pattern A, local mode), confine local shell
            execution to the workspace via an OS kernel sandbox.
        setup_script_path: Path to setup script that was run (if any)
        initial_messages: Optional messages to inject for session continuation
        session_manager: SessionManager for session persistence
        restored_session_data: Tuple of (session_data, warnings, nova_md_loaded) for continuation
        model_provider: Provider recorded on the resumed session, so the TUI
            records that session's provider rather than the global default.
    """
    # Lazy import: core_agent pulls in deepagents (~4s) which we only need once
    # we're actually building the agent, not at CLI startup.
    from novacode_cli.agents.core_agent import create_agent_with_config

    # Create agent with conditional tools.
    # NOTE: several built-in tools are intentionally NOT registered to keep the
    # agent lean — browser automation
    # (browser_automate / capture_browser_console), git tools, and the code-quality
    # tools (lint_code / format_code_file / check_types). LSP tools are likewise
    # not registered. Test running and dev-server management are likewise done via
    # the shell (`execute`) tool, which runs correctly in both local and sandbox
    # modes — so the (previously stubbed) run_tests / *_server tools are not
    # registered either. The agent does all of these via the shell when needed.
    tools = [
        fetch_url,
        # Clarification: ask the user a structured multiple-choice question when a
        # request is ambiguous. Stripped from subagents in _harden_subagent_specs
        # (a question interrupt inside a subagent would crash the turn).
        ask_user_question,
        # Self-planning: the agent can switch itself into plan mode for complex /
        # risky tasks (enter_plan_mode engages read-only enforcement via the agent
        # loop), then present a plan for approval (exit_plan_mode). Both are
        # stripped from subagents in _harden_subagent_specs.
        enter_plan_mode,
        exit_plan_mode,
        # Wiki Tools
        wiki_read,
        wiki_search,
        wiki_update_index,
        wiki_write,
        # Utility tools
        package_info,
        think,
        speak,
        oracle,
        skill_manage,
        
        # Web search (always available, no API key needed)
        duckduckgo_search,
        docs_search,
        # Web scraping (GitHub trending, HN, LinkedIn, Reddit)
        github_trending,
        hacker_news,
        linkedin_jobs,
        reddit_posts,
        # Memory management (persist across sessions)
        write_memory,
        read_memory,
        # Structured durable memory (key/value via the LangGraph store)
        remember,
        recall,
        list_memories,
        forget,
    ]
    # Conditionally add Semble-powered code search tools
    if code_search is not None:
        tools.append(code_search)
    if find_related_code is not None:
        tools.append(find_related_code)
    if settings.has_tavily:
        tools.append(web_search)

    # Initialize file recovery manager for this session
    from novacode_cli.recovery import get_recovery_manager, list_trash, restore_file

    get_recovery_manager(
        session_id=session_state.session_id or session_state.thread_id,
        workspace_root=Path.cwd(),
    )
    tools.extend([list_trash, restore_file])

    # Background-task tools: inspect / control commands detached with Ctrl+B.
    # (No blocking wait — background tasks are fire-and-forget; these are all
    # non-blocking so the agent stays free while a task runs.)
    from novacode_cli.tools.job_tools import (
        get_task_logs,
        get_task_status,
        list_background_tasks,
        restart_task,
        terminate_task,
    )

    tools.extend([
        list_background_tasks,
        get_task_status,
        get_task_logs,
        terminate_task,
        restart_task,
    ])

    # Artifact tools: turn session outputs into live, shareable web pages.
    from novacode_cli.tools.artifact_tools import (
        create_artifact,
        list_artifacts,
        update_artifact,
    )

    tools.extend([create_artifact, update_artifact, list_artifacts])

    # Persistent Python kernel + detached daemon tools. Both are agent-facing
    # tools that must NEVER console.print (they run inside the live agent loop /
    # TUI). The kernel keeps a namespace across calls; the daemon tool launches
    # long-lived background processes tracked in ~/.nova/daemons/.
    from novacode_cli.tools.daemon_tool import daemon
    from novacode_cli.tools.python_kernel_tool import python_kernel

    tools.extend([python_kernel, daemon])

    # Heavy initialization with live animated boot status.
    # The BootAnimation context wraps all boot_status() calls from agent
    # creation, MCP discovery, session restore, and token calculation.
    # transient=True means the animation disappears cleanly before the TUI
    # takes over the terminal.
    from novacode_cli.config.config import BootAnimation

    # Apply the filesystem/write_file patches here (not at module import) so
    # they don't pull deepagents (~4s) at CLI startup. They're only needed once
    # file tools run, which is after the agent is built below.
    from novacode_cli.utils.backend_patches import (
        apply_filesystem_host_path_patch,
        apply_write_file_dict_content_patch,
    )

    apply_filesystem_host_path_patch()
    apply_write_file_dict_content_patch()

    with BootAnimation.start():
        agent, composite_backend = create_agent_with_config(
            model,
            assistant_id,
            tools,
            sandbox=sandbox_backend,
            sandbox_type=sandbox_type,
            store=store,
            checkpointer=checkpointer,
            steering_instructions=session_state.steering_instructions,
            exec_sandbox=exec_sandbox,
            session_id=session_state.session_id or session_state.thread_id,
            # Redundant today (it defaults to the same value), but explicit:
            # a parallel-session child is bound to its git worktree solely by
            # the cwd it was spawned in, and this is where that binding lands.
            workspace_root=str(settings.get_workspace_root()),
        )

        # Set agent context in session state for dynamic model switching
        session_state.set_agent_context(
            agent=agent,
            backend=composite_backend,
            checkpointer=checkpointer,
            store=store,
            tools=tools,
            assistant_id=assistant_id,
            model=model,
            sandbox_type=sandbox_type,
            sandbox_id=getattr(sandbox_backend, "id", None) if sandbox_backend else None,
            sandbox=sandbox_backend,
        )

        # Eagerly preload voice models at the boot banner whenever voice will be
        # used — `enabled` (always-listening) OR `speak_responses` (Nova talks)
        # OR push-to-talk. Gating on `enabled` alone meant PTT / speak-only users
        # paid the (large) model load inline on first use instead of at startup.
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli import audio

        cfg = NovaConfig().get_voice_config()
        # No audio in headless mode — never preload the (large) voice models.
        _voice_wanted = bool(
            not getattr(session_state, "headless", False)
            and (
                cfg.get("enabled")
                or cfg.get("speak_responses")
                or cfg.get("mode") == "push_to_talk"
            )
        )
        if _voice_wanted and audio.is_voice_available():
            boot_status("voice: preloading models (downloading if not present)…")
            try:
                from novacode_cli.audio.pipeline import VoicePipeline

                voice_pipeline = VoicePipeline(
                    stt_provider=cfg.get("stt_provider", "faster-whisper"),
                    tts_provider=cfg.get("tts_provider", "piper"),
                    provider_configs=cfg.get("providers", {}),
                    stt_model=cfg.get("stt_model", "base"),
                    stt_device=cfg.get("stt_device", "auto"),
                    tts_voice=cfg.get("tts_voice", "en_US-lessac-medium"),
                )
                await voice_pipeline.warmup()
                session_state._voice_pipeline = voice_pipeline
                boot_status("voice: stack ready", "ok")
            except Exception as e:
                boot_status(f"voice: warmup failed ({e})", "warn")

    # Wire the SteeringMiddleware's instruction list to the session state.
    #
    # The middleware's `_instructions` list was already set to the shared
    # `session_state.steering_instructions` list during agent creation
    # (create_agent_with_config passes it as `steering_instructions=` to
    # SteeringMiddleware.__init__). No post-hoc wiring is needed — the
    # middleware reads that list on every model call, so /steer and live
    # TUI-steer appends take effect immediately on the agent's next step.

    # Store references on session_state so the remote message processor
    # can access them (it runs as a background task, can't close over locals)
    session_state._console = console
    session_state._composite_backend = composite_backend
    # image_tracker and _seen_message_ids are set on session_state
    # by the TUI (run_tui) since they're not available in this scope

    # Initialize remote bridge infrastructure
    # Queue for messages from Discord/Telegram; processed by a background task
    # NOTE: This must be set up BEFORE the processor task starts, otherwise
    # the processor gets a None reference and silently crashes
    session_state._remote_message_queue: asyncio.Queue = asyncio.Queue()
    session_state._remote_message_lock = asyncio.Lock()  # serialize remote+local agent calls
    from novacode_cli.remote.bridge import RemoteBridgeManager

    session_state._remote_bridge_manager = RemoteBridgeManager(session_state._remote_message_queue)

    # Wire status callback so bridge events (connect, disconnect, messages)
    # show in the local CLI console
    async def _remote_status_callback(msg: str) -> None:
        try:
            console.print(f"  [dim]\U0001f517 Remote: {msg}[/dim]")
        except Exception:
            pass

    session_state._remote_bridge_manager.set_status_callback(_remote_status_callback)

    # Resume any persisted cron jobs (Enhancement 3). The scheduler is a queue
    # *producer*, so it works in both CLI and TUI mode (both consume the same
    # queue). Start, keep only if jobs exist \u2014 users who never use /cron pay
    # nothing for an idle ticker. Skipped in headless (one-shot, no consumer).
    if not getattr(session_state, "headless", False):
        try:
            from novacode_cli.memory.store import get_durable_store as _get_store
            from novacode_cli.remote.scheduler import CronScheduler

            _cron_sched = CronScheduler(
                session_state._remote_message_queue, store=_get_store()
            )
            await _cron_sched.start()
            if _cron_sched.list_jobs():
                session_state._cron_scheduler = _cron_sched
                _proc_logger.info(
                    "Cron scheduler resumed %d job(s)", len(_cron_sched.list_jobs())
                )
            else:
                await _cron_sched.stop()
        except Exception as _cron_exc:
            logging.getLogger("novacode_cli.remote").error(
                f"Failed to resume cron scheduler: {_cron_exc}", exc_info=True
            )

    # Inject initial messages if continuing a session
    if initial_messages:
        config = {"configurable": {"thread_id": session_state.thread_id}}
        await agent.aupdate_state(
            config=config,  # type: ignore
            values={"messages": initial_messages},
            as_node="model",
        )
        boot_status(f"session: {len(initial_messages)} messages restored")

    # Calculate baseline token count for accurate token tracking
    from novacode_cli.agents.core_agent import get_system_prompt

    from .token_utils import calculate_baseline_tokens

    agent_dir = settings.get_agent_dir(assistant_id)
    system_prompt = get_system_prompt(
        assistant_id=assistant_id, sandbox_type=sandbox_type, exec_sandbox=exec_sandbox
    )
    baseline_tokens = calculate_baseline_tokens(model, agent_dir, system_prompt, assistant_id)

    # Extract model name for context window calculation
    model_name = getattr(model, "model_name", None) or getattr(model, "model", "unknown")

    # Parallel-session child: stay up and serve the parent over stdio JSONL.
    # Checked BEFORE the headless branch because a worker also sets `headless`
    # (for its non-interactive guards) but has no one-shot prompt to run.
    if getattr(session_state, "worker", False):
        from novacode_cli.sessions.worker import run_session_worker
        from novacode_cli.ui.ui_elements import TokenTracker

        token_tracker = TokenTracker()
        token_tracker.set_baseline(baseline_tokens)
        if model_name:
            token_tracker.set_model(model_name)
        session_state.token_tracker = token_tracker
        try:
            exit_code = await run_session_worker(
                agent=agent,
                assistant_id=assistant_id,
                session_state=session_state,
                backend=composite_backend,
                model_name=model_name,
                session_manager=session_manager,
            )
        finally:
            await _shutdown_background_services(session_state)
        session_state.headless_exit_code = exit_code
        return

    # Headless (non-interactive) mode: run the single prompt through the shared
    # event stream, format machine-readable output, auto-save, and exit. Shares
    # the same agent/backend/session as the TUI.
    if getattr(session_state, "headless", False):
        from novacode_cli.headless import run_headless
        from novacode_cli.ui.ui_elements import TokenTracker

        token_tracker = TokenTracker()
        token_tracker.set_baseline(baseline_tokens)
        if model_name:
            token_tracker.set_model(model_name)
        session_state.token_tracker = token_tracker
        try:
            exit_code = await run_headless(
                agent=agent,
                assistant_id=assistant_id,
                session_state=session_state,
                backend=composite_backend,
                model_name=model_name,
                session_manager=session_manager,
            )
        finally:
            await _shutdown_background_services(session_state)
        session_state.headless_exit_code = exit_code
        return

    # Textual TUI — the only interactive UI.
    from novacode_cli.input_utils import ImageTracker
    from novacode_cli.tui import run_tui
    from novacode_cli.ui.ui_elements import TokenTracker

    token_tracker = TokenTracker()
    token_tracker.set_baseline(baseline_tokens)
    if model_name:
        token_tracker.set_model(model_name)
    session_state.token_tracker = token_tracker
    # Prior conversation turns to replay into the transcript on resume.
    _restored_msgs = None
    if restored_session_data:
        try:
            _restored_msgs = getattr(restored_session_data[0], "messages", None)
        except Exception:  # noqa: BLE001
            _restored_msgs = None
    # Extract sandbox_id from backend for TUI's session save
    _tui_sandbox_id: str | None = None
    _tui_sandbox_meta: dict | None = None
    if sandbox_backend:
        _tui_sandbox_id = getattr(sandbox_backend, "id", None)
        _tui_sandbox_meta = getattr(sandbox_backend, "_nova_meta", None)

    try:
        await run_tui(
            agent=agent,
            assistant_id=assistant_id,
            session_state=session_state,
            backend=composite_backend,
            token_tracker=token_tracker,
            image_tracker=ImageTracker(),
            model_name=model_name,
            model_provider=model_provider,
            session_manager=session_manager,
            restored_messages=_restored_msgs,
            sandbox_id=_tui_sandbox_id,
            sandbox_type=sandbox_type,
            sandbox_meta=_tui_sandbox_meta,
        )
    except Exception as _crash_exc:
        # Failsafe: session crashed unexpectedly — save whatever we have before dying
        if session_manager and session_state.session_id and session_state.thread_id:
            try:
                console.print("\n[bold yellow]⚠ Unexpected crash — saving session...[/bold yellow]")
                _crash_messages: list = []
                try:
                    _config = {"configurable": {"thread_id": session_state.thread_id}}
                    _snap = await agent.aget_state(_config)  # type: ignore
                    _crash_messages = list(_snap.values.get("messages", []))
                except Exception:
                    pass

                _crash_model = (
                    getattr(model, "model_name", None)
                    or getattr(model, "model", None)
                    or model_name
                )
                _crash_dir = session_manager.save_session(
                    session_id=session_state.session_id,
                    thread_id=session_state.thread_id,
                    messages=_crash_messages,
                    assistant_id=assistant_id,
                    model_name=_crash_model,
                    model_provider=get_current_provider(),
                    project_root=settings.get_workspace_root(),
                    task_status="crashed",
                    sandbox_id=(getattr(sandbox_backend, "id", None) if sandbox_backend else None),
                    sandbox_type=sandbox_type,
                )
                console.print(f"[dim]Session saved → {_crash_dir}[/dim]")
            except Exception as _save_err:
                console.print(f"[dim]Failsafe save failed: {_save_err}[/dim]")
        raise
    finally:
        # Always tear down background services so quitting can't crash on
        # dangling tasks / servers after the TUI exits.
        await _shutdown_background_services(session_state)


def _cleanup_old_checkpoints(checkpoints_dir: Path, max_age_days: int = 30) -> None:
    """Remove checkpoint database files older than max_age_days."""
    import time

    cutoff = time.time() - max_age_days * 86400
    for db_file in checkpoints_dir.glob("*.db"):
        try:
            if db_file.stat().st_mtime < cutoff:
                db_file.unlink(missing_ok=True)
        except OSError:
            pass


async def main(
    assistant_id: str,
    session_state,
    sandbox_type: str = "none",
    sandbox_id: str | None = None,
    setup_script_path: str | None = None,
    continue_session: bool | str = False,
    resume: bool = False,
    ports: dict[int, int] | None = None,
    explicit_sandbox: bool = True,
    sandbox_vcpus: int | None = None,
    sandbox_mem_bytes: int | None = None,
    sandbox_fs_capacity_bytes: int | None = None,
    sandbox_snapshot: str | None = None,
    sandbox_snapshot_id: str | None = None,
) -> None:
    """Main entry point with conditional sandbox support.

    Args:
        assistant_id: Agent identifier for memory storage
        session_state: Session state with auto-approve settings
        sandbox_type: Type of sandbox ("none", "modal", "runloop", "daytona", "docker", "langsmith")
        sandbox_id: Optional existing sandbox ID to reuse
        setup_script_path: Optional path to setup script to run in sandbox
        continue_session: If True, continue last session. If string, use as session ID.
        resume: If True, show interactive session picker to select a session to resume.
        ports: Optional port mapping for Docker sandbox {container_port: host_port}
        explicit_sandbox: Whether the user explicitly chose the sandbox. When False
            (the implicit Docker default), a sandbox-creation failure falls back to
            local mode instead of exiting.
        sandbox_vcpus: Number of virtual CPUs (LangSmith only)
        sandbox_mem_bytes: Memory in bytes (LangSmith only)
        sandbox_fs_capacity_bytes: Filesystem capacity in bytes (LangSmith only)
        sandbox_snapshot: Snapshot name to boot from (LangSmith only)
        sandbox_snapshot_id: Snapshot ID to boot from (LangSmith only)
    """
    # Check path approval before creating any resources (model, sandbox, store,
    # checkpointer, Vixie server, etc.). If the user denies access, nothing
    # expensive has been allocated — no cleanup needed.
    #
    # Headless mode cannot prompt: auto-approve the cwd non-interactively (the
    # user explicitly invoked `nova -p` here) so the run isn't blocked on input.
    if getattr(session_state, "headless", False):
        manager = PathApprovalManager()
        cwd = Path.cwd()
        if not manager.is_path_approved(cwd):
            manager.approve_path(cwd, recursive=True)
    elif not await check_path_approval():
        console.print()
        console.print(
            "[red]Cannot start nova without path approval.[/red]",
            style="dim",
        )
        console.print("[dim]Path approval is required to ensure safe file system access.[/dim]")
        console.print()
        sys.exit(1)

    # Hydrate API keys stored in the OS keychain into the environment so model
    # clients (which read os.environ) can find them — keys may live only in the
    # keychain, not in .env. Must run before create_model() below.
    from novacode_cli.onboarding import load_secrets_into_env

    load_secrets_into_env()

    # Initialize Vixie WebSocket server for desktop pet integration (non-blocking)
    # Server runs in background; if port is in use, it gracefully skips.
    # Skipped in headless mode — it's an interactive desktop-pet feature.
    if not getattr(session_state, "headless", False):
        await start_vixie_server()

    from novacode_cli.session.session_persistence import SessionManager
    from novacode_cli.session.session_restore import restore_session

    # Bound the other unbounded store: session dirs each keep a full
    # archive.jsonl plus large_tool_results/ and conversation_history/.
    # _cleanup_old_checkpoints only prunes checkpoint DBs, so without this the
    # session tree was the largest thing on disk and never shrank.
    try:
        SessionManager().cleanup_old_sessions(max_age_days=180)
    except Exception:  # noqa: BLE001 — retention must never block startup
        logging.getLogger(__name__).debug("Session retention sweep failed", exc_info=True)

    # Durable, SQLite-backed store at ~/.nova/store.db so structured memory
    # written via the LangGraph store survives restarts (falls back to
    # in-memory if SQLite is unavailable). See novacode_cli/memory/store.py.
    from novacode_cli.memory.store import get_async_durable_store

    store = await get_async_durable_store()

    # Use SQLite-backed checkpointer for cross-restart session continuity when available.
    # Falls back to InMemorySaver if langgraph-checkpoint-sqlite is not installed.
    if _SQLITE_CHECKPOINTER_AVAILABLE:
        checkpoints_dir = settings.nova_dir / "checkpoints"  # type: ignore
        checkpoints_dir.mkdir(parents=True, exist_ok=True)
        # Clean up checkpoint DBs older than 30 days to prevent unbounded growth
        _cleanup_old_checkpoints(checkpoints_dir, max_age_days=30)
        _db_path = str(checkpoints_dir / "nova_checkpoints.db")

        async def _setup_sqlite_checkpointer():
            # AsyncSqliteSaver.from_conn_string() is an *async context manager*,
            # not a saver — entering/leaving it would close the connection while
            # the session is still running. Instead, open a persistent aiosqlite
            # connection and build the saver directly so it lives for the whole
            # session (closed implicitly on process exit).
            import aiosqlite

            # Daemonise aiosqlite's worker thread BEFORE it starts (on await):
            # it's non-daemon by default and is never closed, so it blocks the
            # interpreter at exit — making /quit hang. WAL + per-turn commits
            # mean nothing in-flight is lost when the daemon thread is abandoned.
            _conn_cm = aiosqlite.connect(_db_path)
            _conn_cm.daemon = True
            conn = await _conn_cm
            saver = _AsyncSqliteSaver(conn)  # type: ignore[call-arg]
            await saver.setup()
            # Enable WAL mode on the saver's own connection: concurrent reads
            # don't block writes — critical for async streaming + checkpoint writes.
            try:
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.commit()
            except Exception:
                pass  # non-fatal — default journal mode still works
            return saver

    # Initialize session manager for persistence
    session_manager = SessionManager()
    initial_messages: list | None = None

    # Resolve WHICH session we are resuming BEFORE building the model, so the
    # session's own recorded model can be restored. This is filesystem-only
    # (a picker prompt plus a meta.json read), so it costs nothing to hoist.
    if resume:
        from novacode_cli.session.session_restore import select_session_interactive

        selected_id = await select_session_interactive(session_manager)
        if selected_id is None:
            # User cancelled or no sessions available
            return
        # Convert resume selection into a continue_session with the selected ID
        continue_session = selected_id

    # Rebuild the resumed session's model when it recorded one. A session saved
    # before the provider was recorded yields (None, None) and falls through to
    # the global config silently; an unavailable model yields a warning that is
    # surfaced with the other resume warnings.
    session_model = None
    session_model_warning: str | None = None
    session_provider: str | None = None
    # Resolve the session id for BOTH forms of continue: a concrete id, and the
    # bare `--continue` flag (True = "the latest session"). Guarding on
    # isinstance(str) alone skipped the bare flag, so the common
    # `nova --continue` never restored its model at all.
    from novacode_cli.session.session_restore import resolve_resume_session_id

    _resume_session_id = resolve_resume_session_id(
        continue_session=continue_session,
        session_manager=session_manager,
        project_root=settings.get_workspace_root(),
    )

    if _resume_session_id is not None:
        _resume_meta = session_manager.load_session_meta(_resume_session_id)
        if _resume_meta is not None:
            session_provider = getattr(_resume_meta, "model_provider", None)
            session_model, session_model_warning = create_model_for_session(
                session_provider,
                getattr(_resume_meta, "model_name", None),
            )
            # create_model_for_session returns no model for a legacy session;
            # keep the recorded provider only when it actually rebuilt one, so a
            # failed restore never writes a provider the live model is not on.
            if session_model is None:
                session_provider = None

    if _SQLITE_CHECKPOINTER_AVAILABLE:
        # Run model creation (heavy SDK imports) and checkpointer setup (SQLite
        # I/O) concurrently — saves ~1-2s on cold start. A restored session model
        # is already built, so only the checkpointer is awaited in that case.
        if session_model is not None:
            model = session_model
            checkpointer = await _setup_sqlite_checkpointer()
        else:
            model, checkpointer = await asyncio.gather(
                asyncio.to_thread(create_model),
                _setup_sqlite_checkpointer(),
            )
    else:
        checkpointer = InMemorySaver()
        model = session_model or await asyncio.to_thread(create_model)

    # Sandbox identity recovered from a resumed session (for reconnect).
    restored_sandbox_id: str | None = None
    restored_sandbox_type: str | None = None

    # Handle session continuation
    if continue_session:
        from novacode_cli.session.session_prompt_builder import (
            build_continuation_prompt,
            load_NOVA_md,
        )
        from novacode_cli.tracking.workspace_anchoring import (
            detect_drift,
            scan_workspace,
        )

        # The project root, matching how sessions are saved. Keyed on cwd, a
        # session saved from the repo root was not found when resuming from a
        # subfolder (and vice versa).
        project_root = settings.get_workspace_root()
        session_id = continue_session if isinstance(continue_session, str) else None

        result = restore_session(session_manager, session_id, project_root)
        if result:
            session_data, warnings = result

            # Surface a failed model restore alongside the other resume warnings,
            # so the user learns why they are not on the session's model.
            if session_model_warning:
                warnings.append(session_model_warning)

            from novacode_cli.config.config import get_default_coding_instructions

            # Run independent I/O tasks concurrently:
            #  - load recent messages (DB read)
            #  - scan workspace (git subprocesses, cached)
            #  - load Nova.md (filesystem read)
            recent_messages, current_workspace, nova_md_content = await asyncio.gather(
                asyncio.to_thread(
                    session_manager.load_recent_messages, session_data.meta.session_id
                ),
                asyncio.to_thread(scan_workspace, project_root),
                asyncio.to_thread(load_NOVA_md, project_root),
            )

            if session_data.workspace_state:
                drift_warnings = detect_drift(session_data.workspace_state, current_workspace)
                warnings.extend(drift_warnings)

            base_system_prompt = get_default_coding_instructions()

            # Build the continuation prompt from the FULL prior history (archive +
            # recent, already loaded by restore_session). build_continuation_prompt
            # token-budgets it to the most recent ~30k tokens, so this retains far
            # more of the conversation than the 20-message recent window while
            # still bounding context. Fall back to the recent window if the
            # archive wasn't present.
            full_history = list(session_data.messages or [])
            session_data.messages = full_history or recent_messages
            initial_messages = build_continuation_prompt(
                session_data=session_data,
                system_prompt=base_system_prompt,
                NOVA_md_content=nova_md_content,
                workspace_state=current_workspace,
            )

            # Narrow to the recent window for the on-resume transcript replay so
            # the UI isn't flooded with the entire history.
            session_data.messages = recent_messages
            session_data_for_display = session_data

            # Restore session state
            session_state.session_id = session_data.meta.session_id
            session_state.thread_id = session_data.meta.thread_id
            session_state.is_continued = True

            # Recover the sandbox used last time so we can reconnect to its
            # container (preserving installed deps / in-container state).
            restored_sandbox_id = getattr(session_data.meta, "sandbox_id", None)
            restored_sandbox_type = getattr(session_data.meta, "sandbox_type", None)

            # Restore todos if available
            if session_data.todos:
                session_state.todos = session_data.todos

            # Inject session summary as context if available
            if session_data.memory:
                from langchain_core.messages import SystemMessage

                summary_msg = SystemMessage(
                    content=f"## Session Summary (resumed)\n\n{session_data.memory}"
                )
                initial_messages.insert(0, summary_msg)

            # Create tuple for displaying after splash screen
            restored_session_data = (
                session_data_for_display,
                warnings,
                bool(nova_md_content),
            )
        else:
            console.print()
            console.print("[yellow]No previous session found.[/yellow]")
            console.print("[dim]Starting new session.[/dim]")
            console.print()
            restored_session_data = None
    else:
        restored_session_data = None

    async def _run_local_session() -> None:
        """Run the agent locally on the host.

        ``sandbox_type == "os"`` selects Pattern A — files on the host, but shell
        commands confined to the workspace by an OS kernel sandbox. ``"none"``
        (and the Windows Docker-unavailable fallback) runs unconfined.
        """
        # Lazy import: sandbox_factory pulls in deepagents; only needed when
        # actually launching a sandboxed session, not at CLI startup.
        from novacode_cli.integrations.sandbox_factory import create_sandbox

        try:
            await _run_agent_session(
                model,
                assistant_id,
                session_state,
                sandbox_backend=None,
                initial_messages=initial_messages,
                session_manager=session_manager,
                store=store,
                checkpointer=checkpointer,
                restored_session_data=restored_session_data,
                exec_sandbox=(sandbox_type == "os"),
                model_provider=session_provider,
            )
        except KeyboardInterrupt:
            console.print("\n\n[yellow]Interrupted[/yellow]")
            sys.exit(0)
        except Exception as e:
            # Rate limit / API quota errors - show friendly message instead of crash
            if _is_rate_limit_error(e):
                console.print()
                console.print("[bold yellow]Warning: Rate Limit Reached[/bold yellow]")
                console.print("The model provider is rate-limiting requests.")
                console.print(
                    "[dim]Wait a moment and try again, or check your API usage/plan limits.[/dim]"
                )
                console.print()
                sys.exit(2)  # Distinct exit code - caller can retry
            if _is_api_error(e):
                console.print()
                console.print(f"[bold red]API Error[/bold red]: {str(e)[:300]}")
                console.print("[dim]The request failed. Try again or use a different model.[/dim]")
                console.print()
                sys.exit(2)
            console.print("[bold red]Fatal error:[/bold red]", str(e))
            console.print_exception()
            logging.getLogger("novacode_cli").error(
                "Fatal error during session: %s", e, exc_info=True
            )
            dispatch_hook_fire_and_forget(
                HookEvent.ERROR,
                {
                    "error": str(e)[:500],
                    "type": type(e).__name__,
                    "session_id": session_state.session_id,
                },
            )
            if session_state.session_id:
                console.print(
                    f"[dim]Session may have been saved -- resume with:[/dim]\n  nova --continue {session_state.session_id}"
                )
            sys.exit(1)

    # Branch 1: User wants a container/cloud sandbox. "none" and "os" both run
    # locally (os = host files + OS-confined shell) and take the local branch.
    if sandbox_type not in ("none", "os"):
        # Try to create sandbox
        try:
            console.print()
            # If resuming and the user didn't pass an explicit --sandbox-id,
            # reconnect to the container recorded in the saved session (when it
            # was a docker session). create_docker_sandbox self-heals to a fresh
            # container if that one no longer exists.
            effective_sandbox_id = sandbox_id
            if (
                not sandbox_id
                and sandbox_type == "docker"
                and restored_sandbox_type == "docker"
                and restored_sandbox_id
            ):
                effective_sandbox_id = restored_sandbox_id
                boot_status(f"sandbox: reconnecting to docker {restored_sandbox_id[:12]}…")

            sandbox_kwargs = {
                "sandbox_id": effective_sandbox_id,
                "setup_script_path": setup_script_path,
            }
            # Docker-only options: bind-mount, port forwarding, persistence.
            if sandbox_type == "docker":
                if ports:
                    sandbox_kwargs["ports"] = ports  # type: ignore
                # Bind-mount the project so the agent operates on the real files
                # in isolation. Used only on fresh/fallback create; a reconnected
                # container keeps its original mount.
                sandbox_kwargs["mount_dir"] = str(Path.cwd())  # type: ignore
                # Persist the container on exit and tie it to this session so a
                # later resume can reconnect (preserving installed deps/state).
                sandbox_kwargs["persist"] = True  # type: ignore
                sandbox_kwargs["session_id"] = session_state.session_id  # type: ignore

            # LangSmith-specific options: resource config, snapshots, ports.
            if sandbox_type == "langsmith":
                if ports:
                    sandbox_kwargs["ports"] = ports  # type: ignore
                if sandbox_vcpus is not None:
                    sandbox_kwargs["vcpus"] = sandbox_vcpus
                if sandbox_mem_bytes is not None:
                    sandbox_kwargs["mem_bytes"] = sandbox_mem_bytes
                if sandbox_fs_capacity_bytes is not None:
                    sandbox_kwargs["fs_capacity_bytes"] = sandbox_fs_capacity_bytes
                if sandbox_snapshot is not None:
                    sandbox_kwargs["snapshot_name"] = sandbox_snapshot
                if sandbox_snapshot_id is not None:
                    sandbox_kwargs["snapshot_id"] = sandbox_snapshot_id

            with create_sandbox(sandbox_type, **sandbox_kwargs) as sandbox_backend:  # type: ignore
                boot_status(f"sandbox: isolated execution ({sandbox_type})", "ok")
                console.print()

                await _run_agent_session(
                    model,
                    assistant_id,
                    session_state,
                    sandbox_backend,
                    sandbox_type=sandbox_type,
                    setup_script_path=setup_script_path,
                    initial_messages=initial_messages,
                    session_manager=session_manager,
                    store=store,
                    checkpointer=checkpointer,
                    restored_session_data=restored_session_data,
                    model_provider=session_provider,
                )

                # If this run saved no session (e.g. the user exited immediately
                # without a single turn), don't leave the freshly-created sandbox
                # behind — there's no session to ever reconnect it. Vetoing
                # persistence makes create_sandbox remove it on exit instead of
                # accumulating orphaned containers.
                try:
                    if session_manager is not None:
                        _saved = session_manager.load_session(session_state.session_id)
                        if _saved is None or not _saved.messages:
                            sandbox_backend._nova_discard_on_exit = True  # type: ignore[attr-defined]  # noqa: SLF001
                except Exception:  # noqa: BLE001 - never block exit on cleanup hint
                    pass
        except (ImportError, ValueError, RuntimeError, NotImplementedError) as e:
            console.print()
            if not explicit_sandbox:
                # We defaulted to Docker but it isn't available — fall back to
                # local mode instead of blocking the user.
                console.print(f"[yellow]⚠ Docker sandbox unavailable ({e}).[/yellow]")
                console.print(
                    "[dim]Falling back to local execution. "
                    "Start Docker for isolation, or use --no-sandbox to silence this.[/dim]"
                )
                console.print()
                await _run_local_session()
            else:
                # Sandbox was explicitly requested — fail hard (no silent fallback).
                console.print("[red]❌ Sandbox creation failed[/red]")
                console.print(f"[dim]{e}[/dim]")
                sys.exit(1)
        except KeyboardInterrupt:
            console.print("\n\n[yellow]Interrupted[/yellow]")
            sys.exit(0)
        except Exception as e:
            # Rate limit / API quota errors - show friendly message instead of crash
            if _is_rate_limit_error(e):
                console.print()
                console.print("[bold yellow]Warning: Rate Limit Reached[/bold yellow]")
                console.print("The model provider is rate-limiting requests.")
                console.print(
                    "[dim]Wait a moment and try again, or check your API usage/plan limits.[/dim]"
                )
                console.print()
                sys.exit(2)  # Distinct exit code - caller can retry
            if _is_api_error(e):
                console.print()
                console.print(f"[bold red]API Error[/bold red]: {str(e)[:300]}")
                console.print("[dim]The request failed. Try again or use a different model.[/dim]")
                console.print()
                sys.exit(2)
            console.print("[bold red]Fatal error:[/bold red]", str(e))
            console.print_exception()
            logging.getLogger("novacode_cli").error(
                "Fatal error during session: %s", e, exc_info=True
            )
            dispatch_hook_fire_and_forget(
                HookEvent.ERROR,
                {
                    "error": str(e)[:500],
                    "type": type(e).__name__,
                    "session_id": session_state.session_id,
                },
            )
            if session_state.session_id:
                console.print(
                    f"[dim]Session may have been saved -- resume with:[/dim]\n  nova --continue {session_state.session_id}"
                )
            sys.exit(1)

    # Branch 2: User wants local mode (none or default)
    else:
        await _run_local_session()


def _execute_paths_command(args) -> None:
    """Execute paths management commands."""
    manager = PathApprovalManager()

    if args.paths_command == "list":
        approved_paths = manager.list_approved_paths()

        if not approved_paths:
            console.print()
            console.print("[yellow]No approved paths found.[/yellow]")
            console.print(
                "[dim]Paths will be approved automatically when you first run nova in a directory.[/dim]"
            )
            console.print()
            return

        console.print()
        console.print("[bold]Approved Paths:[/bold]", style=COLORS["primary"])
        console.print()

        for path_str, config in approved_paths.items():
            recursive = config.get("recursive", False)
            scope = "📁 + subdirectories" if recursive else "📁 this directory only"

            console.print(f"  {path_str}")
            console.print(f"    [dim]{scope}[/dim]")
            console.print()

    elif args.paths_command == "revoke":
        path = Path(args.path).resolve()
        if manager.revoke_path(path):
            console.print()
            console.print("✅ ", style="green", end="")
            console.print(f"[green]Revoked approval for:[/green] {path}")
            console.print()
        else:
            console.print()
            console.print("⚠️  ", style="yellow", end="")
            console.print(f"[yellow]Path not found in approved list:[/yellow] {path}")
            console.print()

    elif args.paths_command == "clear":
        from prompt_toolkit import prompt

        console.print()
        console.print("[yellow]⚠ This will clear ALL approved paths.[/yellow]")
        console.print("[dim]You'll need to re-approve paths when you next run nova.[/dim]")
        console.print()

        confirm = prompt("Are you sure? (yes/no): ").strip().lower()
        if confirm in ["yes", "y"]:
            # Clear all paths
            manager._approved_paths = {}
            manager._save_approved_paths()
            console.print()
            console.print("✅ ", style="green", end="")
            console.print("[green]All approved paths cleared.[/green]")
            console.print()
        else:
            console.print()
            console.print("[dim]Cancelled.[/dim]")
            console.print()
    else:
        console.print()
        console.print("[yellow]Please specify a subcommand: list, revoke, or clear[/yellow]")
        console.print()
        console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
        console.print("  nova paths list         List all approved paths")
        console.print("  nova paths revoke PATH  Revoke approval for a path")
        console.print("  nova paths clear        Clear all approved paths")
        console.print()


def _execute_config_command(args) -> None:
    """Execute config command to view/edit configuration."""
    import json

    config_file = HOME_DIR / "config.json"
    command = args.config_command

    if command == "show":
        # Show current configuration (non-secret only)
        if config_file.exists():
            console.print()
            console.print("[bold]Current Configuration:[/bold]")
            console.print()
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
                from rich.syntax import Syntax

                syntax = Syntax(
                    json.dumps(config, indent=2),
                    "json",
                    theme="monokai",
                    line_numbers=True,
                )
                console.print(syntax)
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]✗ Error reading config: {e}[/red]")
            console.print()
        else:
            console.print()
            console.print("[yellow]⚠ No configuration file found[/yellow]")
            console.print("[dim]Run 'nova init' to set up configuration[/dim]")
            console.print()

    elif command == "get":
        # Get specific configuration value
        if not args.key:
            console.print("[red]✗ Key required for 'get' command[/red]")
            console.print("[dim]Usage: nova config get <key>[/dim]")
            return

        if config_file.exists():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
                value = config.get(args.key)
                if value is not None:
                    console.print()
                    console.print(f"[bold]{args.key}:[/bold] {value}")
                    console.print()
                else:
                    console.print()
                    console.print(f"[yellow]⚠ Key '{args.key}' not found[/yellow]")
                    console.print()
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]✗ Error reading config: {e}[/red]")
        else:
            console.print("[yellow]⚠ No configuration file found[/yellow]")

    elif command == "set":
        # Set configuration value
        if not args.key or not args.value:
            console.print("[red]✗ Both key and value required for 'set' command[/red]")
            console.print("[dim]Usage: nova config set <key> <value>[/dim]")
            return

        config = {}
        if config_file.exists():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001, S110
                pass

        # Parse value (try JSON first, then string)
        try:
            parsed_value = json.loads(args.value)
        except json.JSONDecodeError:
            parsed_value = args.value

        config[args.key] = parsed_value
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

        console.print()
        console.print(f"[green]✓ Set {args.key} = {parsed_value}[/green]")
        console.print()


def _execute_secrets_command(args) -> None:
    """Execute secrets command to manage API keys."""
    from prompt_toolkit import prompt

    from novacode_cli.onboarding import SecretManager

    secret_manager = SecretManager()
    command = args.secrets_command

    if command == "list":
        # List all stored secrets
        secrets = secret_manager.list_secrets()
        console.print()
        if secrets:
            console.print("[bold]Configured API keys:[/bold]")
            for secret in secrets:
                # Display without revealing values
                display_name = secret.replace("_api_key", "").replace("_", " ").title()
                console.print(f"  • {display_name} ({secret})")
        else:
            console.print("[yellow]⚠ No API keys configured[/yellow]")
            console.print("[dim]Use 'nova secrets set <key>' to add API keys[/dim]")
        console.print()

    elif command == "set":
        # Set API key
        if not args.key:
            console.print("[red]✗ Key name required for 'set' command[/red]")
            console.print("[dim]Usage: nova secrets set <key> (e.g., 'openai_api_key')[/dim]")
            return

        console.print()
        console.print(f"[bold]Setting {args.key}:[/bold]")
        api_key = prompt("Enter API key: ", is_password=True).strip()

        if api_key:
            if secret_manager.store_secret(args.key, api_key):
                console.print()
                console.print("[green]✓ API key saved to system keychain[/green]")
                console.print()
            else:
                console.print()
                console.print("[red]✗ Failed to save API key[/red]")
                console.print()
        else:
            console.print()
            console.print("[yellow]⚠ No API key provided, cancelled[/yellow]")
            console.print()

    elif command == "delete":
        # Delete API key
        if not args.key:
            console.print("[red]✗ Key name required for 'delete' command[/red]")
            console.print("[dim]Usage: nova secrets delete <key> (e.g., 'openai_api_key')[/dim]")
            return

        console.print()
        console.print(f"[yellow]⚠ Delete API key '{args.key}'?[/yellow]")
        confirm = prompt("Continue? [y/N]: ").strip().lower()

        if confirm == "y":
            if secret_manager.delete_secret(args.key):
                console.print()
                console.print(f"[green]✓ API key '{args.key}' deleted[/green]")
                console.print()
            else:
                console.print()
                console.print("[red]✗ Failed to delete API key[/red]")
                console.print()
        else:
            console.print()
            console.print("[dim]Cancelled[/dim]")
            console.print()


def _run_onboarding() -> bool:
    """Run first-run/reset onboarding via the native Textual screen."""
    from novacode_cli.tui.pickers import run_onboarding_tui

    return run_onboarding_tui()


def _resolve_headless_prompt(print_arg) -> str:
    """Resolve the headless prompt from the --print value or stdin.

    ``print_arg`` is the argparse value: a string (``-p "..."``) or ``True``
    (bare ``-p``, read from stdin). Exits with a clear error if no prompt can
    be obtained (e.g. bare ``-p`` on an interactive terminal with no pipe).
    """
    if isinstance(print_arg, str):
        prompt = print_arg.strip()
        if prompt:
            return prompt

    # Bare -p (or empty value): read the prompt from stdin when piped.
    if not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if prompt:
            return prompt

    print(
        "Error: --print requires a prompt. Pass it as an argument "
        '(nova -p "your prompt") or pipe it via stdin (echo "..." | nova -p).',
        file=sys.stderr,
    )
    sys.exit(1)


def _setup_headless_io() -> int | None:
    """Prepare stdout/stderr for a headless run; return a result fd.

    Two things must hold in headless mode:

    1. stdout must carry *only* the machine-readable result. All boot / status /
       diagnostic output uses the module-global Rich ``console``, so point it at
       stderr (already UTF-8 wrapped at import on Windows).
    2. The result must survive side effects of agent build — notably stdio MCP
       servers (e.g. Serena) that close the Python-level ``sys.stdout`` object on
       shutdown. So duplicate fd 1 *now* (before any MCP runs) and write results
       to that independent descriptor via ``os.write``; closing ``sys.stdout``
       (the Python object) does not close this dup'd fd.

    Returns the dup'd fd, or ``None`` if it can't be duplicated (the runner then
    falls back to ``sys.stdout``).
    """
    console.file = sys.stderr
    try:
        return os.dup(sys.stdout.fileno())
    except (OSError, ValueError, io.UnsupportedOperation):
        return None


def cli_main() -> None:
    """Entry point for console script."""
    # Lazy imports: sandbox_factory pulls in deepagents; only needed when
    # actually resolving/launching a sandbox, not at CLI startup.
    from novacode_cli.integrations.sandbox_factory import (
        parse_ports,
        resolve_sandbox_type,
    )

    # Fix for gRPC fork issue on macOS
    # https://github.com/grpc/grpc/issues/37642
    if sys.platform == "darwin":
        os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"

    # Check dependencies first
    check_cli_dependencies()

    try:
        args = parse_args()

        # Headless (non-interactive) mode: resolve the prompt now and route all
        # Rich console output to stderr so stdout carries only the result.
        headless_prompt: str | None = None
        headless_out_fd: int | None = None
        if getattr(args, "print_prompt", None) is not None:
            headless_prompt = _resolve_headless_prompt(args.print_prompt)
            headless_out_fd = _setup_headless_io()

        # First-run detection (skip for init and doctor commands)
        if args.command not in ["init", "doctor", "help"]:
            if not settings.get_onboarding_status():
                if headless_prompt is not None:
                    print(
                        "Error: Nova is not configured. Run 'nova init' first.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                console.print()
                console.print("[yellow]→ First run detected[/yellow]")
                console.print()

                if _run_onboarding():
                    console.print()
                    console.print(
                        "[dim]You can now run your command or start an interactive session.[/dim]"
                    )
                    console.print()
                else:
                    console.print()
                    console.print("[red]✗ Setup incomplete[/red]")
                    console.print("[dim]Run 'nova init --reset' to try again[/dim]")
                    console.print()
                    sys.exit(1)

        if args.command == "init":
            # Check if --reset flag is set (re-run onboarding)
            if args.reset:
                console.print()
                console.print("[yellow]⚠ This will overwrite your current configuration.[/yellow]")
                from prompt_toolkit import prompt

                confirm = prompt("Continue? [y/N]: ").strip().lower()
                if confirm == "y":
                    _run_onboarding()
                else:
                    console.print("[dim]Cancelled.[/dim]")

        elif args.command == "help":
            show_help()
        elif args.command == "list":
            from novacode_cli.agents.core_agent import list_agents

            list_agents()
        elif args.command == "reset":
            from novacode_cli.agents.core_agent import reset_agent

            reset_agent(args.agent, args.source_agent)
        elif args.command == "skills":
            execute_skills_command(args)
        elif args.command == "mcp":
            execute_mcp_command(args)
        elif args.command == "paths":
            _execute_paths_command(args)
        elif args.command == "migrate":
            if args.check:
                check_migration_status()
            else:
                migrate_agents()
        elif args.command == "config":
            _execute_config_command(args)
        elif args.command == "secrets":
            _execute_secrets_command(args)
        elif args.command == "doctor":
            from novacode_cli.doctor import run_doctor  # lazy: ~1s import, niche command

            sys.exit(run_doctor())
        else:
            # Create session state from args
            session_state = SessionState(
                auto_approve=args.auto_approve,
                no_splash=args.no_splash or headless_prompt is not None,
            )
            # The TUI is the only interactive UI; `headless` selects the
            # non-interactive path (see the branch order in main()).
            session_state.headless = headless_prompt is not None

            # Parallel-session child. `headless` is reused as the umbrella
            # "non-interactive process" flag — it already suppresses the splash,
            # the voice preload, the Vixie server and the cron scheduler, all of
            # which would fight the parent over the terminal, devices and ports.
            # auto_approve is deliberately NOT set: a human IS watching, in the
            # parent, and interrupts are forwarded there for a real decision.
            if getattr(args, "session_worker", False):
                session_state.headless = True
                session_state.worker = True
                session_state.no_splash = True
                session_state.headless_out_fd = _setup_headless_io()
                if args.session_id:
                    session_state.session_id = args.session_id
            if headless_prompt is not None:
                session_state.headless_prompt = headless_prompt
                session_state.headless_output_format = args.output_format
                session_state.headless_max_turns = args.max_turns
                session_state.headless_deny_tools = args.deny_tools
                session_state.headless_out_fd = headless_out_fd
                # No human to approve tools — auto-approve unless --deny-tools.
                # Sandbox / dangerous-command guardrails still apply.
                if not args.deny_tools:
                    session_state.auto_approve = True

            # Ensure the project wiki vault exists from session start, so the user
            # can point Obsidian at .nova/wiki/ without first running a wiki
            # command (the Web Clipper drops into .nova/wiki/Clippings/).
            # Best-effort — skipped silently outside a git project or on any error.
            try:
                from novacode_cli.wiki.manager import WikiManager

                WikiManager().ensure_structure()
            except Exception:  # noqa: BLE001 — never block startup on the wiki
                pass

            # Parse port forwarding argument
            ports = parse_ports(args.ports)

            # Resolve the effective sandbox and whether the user chose it
            # explicitly. Default is OS-confined local execution (Pattern A) on
            # Linux/macOS, and plain host execution + approvals on Windows. An
            # explicit choice (or --no-sandbox) is honored without fallback.
            try:
                sandbox_type, explicit_sandbox = resolve_sandbox_type(args.sandbox, args.no_sandbox)
            except ValueError as e:
                console.print(f"[red]Error: {e}[/red]")
                sys.exit(1)

            # --resume and --continue are mutually exclusive
            if args.resume and args.continue_session:
                console.print("[red]Error: --resume and --continue are mutually exclusive.[/red]")
                console.print(
                    "[dim]Use --resume to pick a session interactively, or --continue <id> to resume a specific session.[/dim]"
                )
                sys.exit(1)

            # API key validation happens in create_model()
            asyncio.run(
                main(
                    args.agent,
                    session_state,
                    sandbox_type,
                    args.sandbox_id,
                    args.sandbox_setup,
                    args.continue_session,
                    resume=args.resume,
                    ports=ports,
                    explicit_sandbox=explicit_sandbox,
                    sandbox_vcpus=args.sandbox_vcpus,
                    sandbox_mem_bytes=args.sandbox_mem_bytes,
                    sandbox_fs_capacity_bytes=args.sandbox_fs_capacity_bytes,
                    sandbox_snapshot=args.sandbox_snapshot,
                    sandbox_snapshot_id=args.sandbox_snapshot_id,
                )
            )
            # main() returned normally — every teardown step has run (session
            # saved, sandbox stopped, background tasks cancelled, connections
            # committed). Force a prompt process exit so a lingering non-daemon
            # thread (sqlite/aiosqlite worker, docker SDK pool, etc.) can't hang
            # /quit during interpreter shutdown. Error paths use sys.exit() and
            # propagate before reaching here, so exit codes are preserved.
            #
            # Flushes are best-effort: a stdio MCP server (e.g. Serena) may have
            # already closed sys.stdout during a headless run, so a closed-stream
            # error here must not stop os._exit() from running (which would let
            # non-daemon threads hang the process).
            for _std in (sys.stdout, sys.stderr):
                try:
                    _std.flush()
                except (ValueError, OSError):
                    pass
            os._exit(getattr(session_state, "headless_exit_code", 0))
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C - suppress ugly traceback
        console.print("\n\n[yellow]Interrupted[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    cli_main()
