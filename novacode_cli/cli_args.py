"""CLI argument definitions, independent of configuration and agent runtime."""

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from novacode_cli._version import __version__
from novacode_cli.brand import format_version_banner


class _SkillsCommandParser(argparse.ArgumentParser):
    """Let upstream parse its own flags, including options before the source."""

    upstream_add = False

    def parse_known_args(
        self,
        args: Sequence[str] | None = None,
        namespace: Any = None,  # noqa: ANN401 — argparse supports caller-defined namespaces
    ) -> tuple[Any, list[str]]:
        """Keep the add command's argv intact for the official parser."""
        if not self.upstream_add:
            return super().parse_known_args(args, namespace)
        namespace = namespace or argparse.Namespace()
        namespace.upstream_args = list(args) if args is not None else sys.argv[1:]
        return namespace, []


def setup_skills_parser(
    subparsers: Any,  # noqa: ANN401 — argparse's heterogeneous subparser action
) -> argparse.ArgumentParser:
    """Setup the skills subcommand parser with all its subcommands."""
    skills_parser = subparsers.add_parser(
        "skills",
        help="Manage agent skills",
        description="Manage agent skills - create, list, and view skill information",
    )
    skills_subparsers = skills_parser.add_subparsers(
        dest="skills_command",
        help="Skills command",
        parser_class=_SkillsCommandParser,
    )

    # Skills list
    list_parser = skills_subparsers.add_parser(
        "list",
        help="List all available skills",
        description="List all available skills",
    )
    list_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    list_parser.add_argument(
        "--project",
        action="store_true",
        help="Show only project-level skills",
    )
    list_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Show only global skills (user-level)",
    )

    # Skills create
    create_parser = skills_subparsers.add_parser(
        "create",
        help="Create a new skill",
        description="Create a new skill with a template SKILL.md file",
    )
    create_parser.add_argument("name", help="Name of the skill to create (e.g., web-research)")
    create_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    create_parser.add_argument(
        "--project",
        action="store_true",
        help="Create skill in project directory instead of user directory",
    )
    create_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Create skill in global directory (user-level)",
    )

    # Skills info
    info_parser = skills_subparsers.add_parser(
        "info",
        help="Show detailed information about a skill",
        description="Show detailed information about a specific skill",
    )
    info_parser.add_argument("name", help="Name of the skill to show info for")
    info_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    info_parser.add_argument(
        "--project",
        action="store_true",
        help="Search only in project skills",
    )
    info_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Search only in global skills (user-level)",
    )

    # The add subcommand belongs to upstream; retain every argument verbatim.
    add_parser = skills_subparsers.add_parser(
        "add",
        help="Install skills using the official Skills CLI (requires Node.js/npx)",
        add_help=False,
    )
    add_parser.upstream_add = True
    # Skills remove
    remove_parser = skills_subparsers.add_parser(
        "remove",
        help="Remove an installed skill",
        description="Remove an installed skill and its directory",
    )
    remove_parser.add_argument(
        "name",
        help="Name of the skill to remove",
    )
    remove_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    remove_parser.add_argument(
        "--project",
        action="store_true",
        help="Remove from project skills directory",
    )
    remove_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Remove from global skills directory",
    )
    remove_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )

    # Skills update
    update_parser = skills_subparsers.add_parser(
        "update",
        help="Update skill(s) from their original source",
        description="Re-fetch and reinstall a skill from its original GitHub URL",
    )
    update_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Name of the skill to update (omit when using --all)",
    )
    update_parser.add_argument(
        "--agent",
        default="nova-agent",
        help="Agent identifier for skills (default: nova-agent)",
    )
    update_parser.add_argument(
        "--project",
        action="store_true",
        help="Target project skills directory",
    )
    update_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Target global skills directory",
    )
    update_parser.add_argument(
        "--all",
        dest="all_skills",
        action="store_true",
        help="Update all skills that have a lock entry",
    )

    # Skills find
    find_parser = skills_subparsers.add_parser(
        "find",
        help="Search GitHub for skills",
        description="Search GitHub for SKILL.md repositories matching a query",
    )
    find_parser.add_argument(
        "query",
        help="Search term (e.g. 'azure', 'kubernetes', 'pdf')",
    )

    # Skills search (alias for find)
    search_parser = skills_subparsers.add_parser(
        "search",
        help="Search GitHub for skills (alias for find)",
        description="Search GitHub for SKILL.md repositories matching a query",
    )
    search_parser.add_argument(
        "query",
        help="Search term (e.g. 'azure', 'kubernetes', 'pdf')",
    )

    return skills_parser


def setup_mcp_parser(subparsers: Any) -> argparse.ArgumentParser:  # noqa: ANN401
    """Setup the MCP subcommand parser with all its subcommands.

    Args:
        subparsers: The subparsers object from argparse

    Returns:
        The MCP parser instance
    """
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Manage MCP (Model Context Protocol) servers",
        description="Manage MCP servers - add, remove, list, and install servers",
    )
    mcp_subparsers = mcp_parser.add_subparsers(
        dest="mcp_command",
        help="MCP command",
    )

    # MCP add
    add_parser = mcp_subparsers.add_parser(
        "add",
        help="Add an MCP server",
        description="Add or update an MCP server configuration",
    )
    add_parser.add_argument("name", help="Server name/identifier")
    add_parser.add_argument(
        "--transport",
        required=True,
        choices=["http", "stdio"],
        help="Transport type (http or stdio)",
    )
    add_parser.add_argument(
        "--url",
        help="Server URL (required for HTTP transport)",
    )
    add_parser.add_argument(
        "--command",
        help="Command to execute (required for stdio transport)",
    )
    add_parser.add_argument(
        "--args",
        nargs="*",
        help="Command arguments (for stdio transport)",
    )
    add_parser.add_argument(
        "--env",
        action="append",
        help="Environment variables in KEY=VALUE format (can be specified multiple times)",
    )
    add_parser.add_argument(
        "--description",
        help="Server description",
    )

    # MCP remove
    remove_parser = mcp_subparsers.add_parser(
        "remove",
        help="Remove an MCP server",
        description="Remove an MCP server configuration",
    )
    remove_parser.add_argument("name", help="Server name/identifier to remove")

    # MCP enable
    enable_parser = mcp_subparsers.add_parser(
        "enable",
        help="Enable a configured MCP server",
        description="Enable a configured MCP server configuration",
    )
    enable_parser.add_argument("name", help="Server name/identifier to enable")

    # MCP disable
    disable_parser = mcp_subparsers.add_parser(
        "disable",
        help="Disable a configured MCP server",
        description="Disable a configured MCP server configuration",
    )
    disable_parser.add_argument("name", help="Server name/identifier to disable")

    # MCP list
    mcp_subparsers.add_parser(
        "list",
        help="List all MCP servers",
        description="List all configured MCP servers",
    )

    # MCP install
    install_parser = mcp_subparsers.add_parser(
        "install",
        help="Install an MCP server from URL",
        description="Auto-discover and install an MCP server from a URL",
    )
    install_parser.add_argument("url", help="URL to discover the MCP server from")
    install_parser.add_argument(
        "--name",
        help="Custom name for the server (auto-detected if not provided)",
    )

    return mcp_parser


def _add_agent_server_args(parser: argparse.ArgumentParser) -> None:
    """The local-agent-server flags, kept out of parse_args' statement count."""
    parser.add_argument(
        "--no-agent-server",
        action="store_true",
        help="Do not launch a local LangGraph server for the async subagents",
    )
    parser.add_argument(
        "--agent-server-port",
        type=int,
        default=None,
        help="Port for the local agent server (default: a free ephemeral port)",
    )


def parse_args() -> argparse.Namespace:  # noqa: PLR0912, PLR0915 — one existing CLI definition
    """Parse command line arguments."""
    from novacode_cli.skills.upstream import add_arguments

    upstream_args = add_arguments(sys.argv[1:])
    if upstream_args is not None:
        return argparse.Namespace(
            command="skills",
            skills_command="add",
            upstream_args=upstream_args,
        )
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
    update_parser = subparsers.add_parser("update", help="Update Nova or check for new code")
    update_parser.add_argument("--check", action="store_true", help="Check without installing")

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
    from novacode_cli.headless.input import positive_int, positive_seconds

    parser.add_argument(
        "--mode",
        choices=("tui", "pipe"),
        default="tui",
        help="Persistent JSONL interface for remote-app bridges: --mode pipe",
    )
    parser.add_argument("--headless", action="store_true", help="Alias for --mode pipe")
    parser.add_argument("--input-format", choices=("jsonl", "text"), default="jsonl")
    parser.add_argument("--startup-timeout", type=positive_seconds, default=120)
    parser.add_argument("--request-timeout", type=positive_seconds, default=None)
    parser.add_argument("--approval-timeout", type=positive_seconds, default=300)
    parser.add_argument("--model", help="Process-local provider:model override")

    parser.add_argument(
        "--print",
        "-p",
        dest="print_prompt",
        nargs="?",
        const=True,
        default=None,
        help="Run a single prompt non-interactively and exit. Pass the prompt as "
        'the value (nova -p "..."), or omit it to read the prompt from stdin '
        '(echo "..." | nova -p).',
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
        type=positive_int,
        default=None,
        help="Headless only: cap observed main-agent turns. The run "
        "stops with a max-turns error if exceeded.",
    )
    parser.add_argument(
        "--deny-tools",
        action="store_true",
        help="Headless only: auto-reject tool approvals (fail-closed) instead of "
        "approving. This rejects approvals; it does not disable every tool.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=None,
        help="Headless only: deadline in seconds, including agent startup.",
    )
    parser.add_argument(
        "--include-partial-messages",
        action="store_true",
        help="Emit text_delta/text_discard events with --output-format stream-json.",
    )
    parser.add_argument(
        "--trust-workspace",
        action="store_true",
        help="Headless only: explicitly grant folder-only trust to the current directory.",
    )
    # Internal: how a parent Nova TUI launches a parallel session. The child
    # speaks JSONL on stdio (see novacode_cli.sessions.worker) and is bound to
    # its own git worktree purely by the cwd it is spawned in. Not for humans.
    parser.add_argument(
        "--safe-ui",
        action="store_true",
        help="Start with the default UI, skipping saved customizations",
    )
    parser.add_argument(
        "--import",
        dest="import_provider",
        help="Import a local conversation (codex, claude, nova, or an installed adapter)",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Import the selected provider's latest session",
    )
    parser.add_argument("--import-session", help="Source session ID or transcript file path")
    parser.add_argument("--import-mode", choices=("compact", "full", "relevant"), default="compact")
    parser.add_argument("--session-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session-id", default=None, help=argparse.SUPPRESS)
    _add_agent_server_args(parser)
    parser.add_argument(
        "--version",
        action="version",
        version=format_version_banner(__version__),
        help="Show the version number and exit",
    )
    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit")

    args = parser.parse_args()
    if args.headless:
        args.mode = "pipe"
    if args.mode == "pipe" and (
        args.print_prompt is not None
        or args.command
        or args.session_worker
        or args.resume
        or args.import_provider
    ):
        parser.error(
            "Pipe mode cannot use --print, subcommands, --session-worker, --resume, "
            "or --import; resume with --continue <id>"
        )
    if args.model and (":" not in args.model or not all(args.model.split(":", 1))):
        parser.error("--model requires provider:model")
    if args.mode == "pipe" and (args.timeout or args.max_turns or args.include_partial_messages):
        parser.error(
            "Pipe mode uses --request-timeout; --timeout, --max-turns and "
            "--include-partial-messages are one-shot options"
        )
    if args.print_prompt is not None and (args.command is not None or args.session_worker):
        parser.error("--print cannot be combined with a subcommand or --session-worker")
    if args.print_prompt is not None and args.resume:
        parser.error(
            "--resume opens an interactive picker; use --continue <session-id> with --print"
        )
    if (
        args.print_prompt is None
        and args.mode != "pipe"
        and (
            args.timeout is not None
            or args.trust_workspace
            or args.include_partial_messages
            or args.max_turns is not None
            or args.deny_tools
        )
    ):
        parser.error("--timeout, --trust-workspace, and --include-partial-messages require --print")
    if args.include_partial_messages and args.output_format != "stream-json":
        parser.error("--include-partial-messages requires --output-format stream-json")
    if args.import_provider:
        if args.resume or args.continue_session:
            parser.error("--import cannot be combined with --resume or --continue")
        if bool(args.latest) == bool(args.import_session):
            parser.error("--import requires exactly one of --latest or --import-session <id/file>")
    elif args.latest or args.import_session:
        parser.error("--latest and --import-session require --import <provider>")
    return args
