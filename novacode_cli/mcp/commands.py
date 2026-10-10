"""CLI commands for MCP server management.

These commands are registered with the CLI via main.py:
- Nova mcp add <name> --transport <type> <connection_details>
- Nova mcp remove <name>
- Nova mcp list
- Nova mcp install <url>
"""

from novacode_cli.cli_args import setup_mcp_parser

import argparse
import sys
import urllib.request
from pathlib import Path
from typing import Any

from novacode_cli.config.config import COLORS, console
from novacode_cli.mcp.config import MCPConfig, MCPServerConfig


def _add(
    name: str,
    transport: str,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    description: str | None = None,
) -> None:
    """Add or update an MCP server configuration.

    Args:
        name: Server name/identifier
        transport: Transport type (http or stdio)
        url: Server URL (required for HTTP transport)
        command: Command to execute (required for stdio transport)
        args: Command arguments (optional, for stdio transport)
        env: Environment variables (optional)
        description: Server description (optional)
    """
    try:
        config = MCPServerConfig(
            transport=transport,  # type: ignore[arg-type]
            url=url,
            command=command,
            args=args or [],
            env=env or {},
            description=description,
        )

        mcp_config = MCPConfig()
        mcp_config.add_server(name, config)

        console.print(
            f"✓ MCP server '{name}' added successfully!",
            style=COLORS["primary"],
        )
        console.print(f"Transport: {transport}", style=COLORS["dim"])
        if url:
            console.print(f"URL: {url}", style=COLORS["dim"])
        if command:
            console.print(f"Command: {command}", style=COLORS["dim"])
            if args:
                console.print(f"Args: {' '.join(args)}", style=COLORS["dim"])
        console.print(
            f"\nConfiguration saved to: {mcp_config.config_path}",
            style=COLORS["dim"],
        )

    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] Invalid configuration: {e}")
        sys.exit(1)


def _remove(name: str) -> None:
    """Remove an MCP server configuration.

    Args:
        name: Server name/identifier
    """
    mcp_config = MCPConfig()

    if mcp_config.remove_server(name):
        console.print(
            f"✓ MCP server '{name}' removed successfully!",
            style=COLORS["primary"],
        )
    else:
        console.print(
            f"[bold red]Error:[/bold red] MCP server '{name}' not found.",
        )
        console.print("\n[dim]Available servers:[/dim]", style=COLORS["dim"])
        servers = mcp_config.list_servers()
        if servers:
            for server_name in servers:
                console.print(f"  - {server_name}", style=COLORS["dim"])
        else:
            console.print("  (none)", style=COLORS["dim"])
        sys.exit(1)


def _enable(name: str) -> None:
    """Enable a configured MCP server.

    Args:
        name: Server name/identifier
    """
    mcp_config = MCPConfig()
    servers = mcp_config.list_servers()
    if name not in servers:
        console.print(
            f"[bold red]Error:[/bold red] MCP server '{name}' not found.",
        )
        sys.exit(1)

    config = servers[name]
    if not getattr(config, "disabled", False):
        console.print(
            f"MCP server '{name}' is already enabled.",
            style=COLORS["primary"],
        )
        return

    config.disabled = False
    mcp_config.add_server(name, config)
    console.print(
        f"✓ MCP server '{name}' enabled successfully!",
        style=COLORS["primary"],
    )


def _disable(name: str) -> None:
    """Disable a configured MCP server.

    Args:
        name: Server name/identifier
    """
    mcp_config = MCPConfig()
    servers = mcp_config.list_servers()
    if name not in servers:
        console.print(
            f"[bold red]Error:[/bold red] MCP server '{name}' not found.",
        )
        sys.exit(1)

    config = servers[name]
    if getattr(config, "disabled", False):
        console.print(
            f"MCP server '{name}' is already disabled.",
            style=COLORS["primary"],
        )
        return

    config.disabled = True
    mcp_config.add_server(name, config)
    console.print(
        f"✓ MCP server '{name}' disabled successfully!",
        style=COLORS["primary"],
    )


def _list() -> None:
    """List all configured MCP servers with connection status."""
    mcp_config = MCPConfig()
    servers = mcp_config.list_servers()

    if not servers:
        console.print("[yellow]No MCP servers configured.[/yellow]")
        console.print(
            "\n[dim]Add a server with:[/dim]",
            style=COLORS["dim"],
        )
        console.print(
            "  Nova mcp add <name> --transport http --url <url>",
            style=COLORS["dim"],
        )
        console.print(
            "  Nova mcp add <name> --transport stdio --command <cmd>",
            style=COLORS["dim"],
        )
        return

    # Get active servers from MCP middleware
    active_servers = set()
    try:
        from novacode_cli.mcp import get_shared_mcp_middleware

        middleware = get_shared_mcp_middleware()

        # Tools are lazily discovered on first model call; force discovery now so
        # the status display reflects actual connectivity rather than "all inactive".
        if not middleware._tools_discovered:
            middleware._discover_tools_sync()

        for tool_meta in middleware._tools_cache:
            server_name = tool_meta.get("server")
            if server_name:
                active_servers.add(server_name)
    except Exception:
        pass

    console.print("\n[bold]Configured MCP Servers:[/bold]\n", style=COLORS["primary"])

    for name, config in servers.items():
        # Show active indicator
        is_disabled = getattr(config, "disabled", False)
        is_active = name in active_servers and not is_disabled
        if is_disabled:
            status_indicator = "[dim]✗[/dim]"
            status_text = "[dim]disabled[/dim]"
        else:
            status_indicator = "[green]✓[/green]" if is_active else "[red]✗[/red]"
            status_text = "[green]active[/green]" if is_active else "[red]inactive[/red]"
        
        console.print(
            f"  {status_indicator} [bold]{name}[/bold] ({status_text})",
            style=COLORS["primary"],
        )
        
        if config.description:
            console.print(f"    {config.description}", style=COLORS["dim"])

        console.print(f"    Transport: {config.transport}", style=COLORS["dim"])

        if config.transport == "http" and config.url:
            console.print(f"    URL: {config.url}", style=COLORS["dim"])
        elif config.transport == "stdio" and config.command:
            console.print(f"    Command: {config.command}", style=COLORS["dim"])
            if config.args:
                console.print(
                    f"    Args: {' '.join(config.args)}",
                    style=COLORS["dim"],
                )

        if config.env:
            # Hide sensitive values (API keys, secrets, etc.)
            console.print("    Environment:", style=COLORS["dim"])
            for key, value in config.env.items():
                # Mask sensitive values
                if any(sensitive in key.upper() for sensitive in ["KEY", "SECRET", "TOKEN", "PASSWORD"]):
                    masked_value = value[:8] + "..." if len(value) > 8 else "***"
                    console.print(f"      {key}={masked_value}", style=COLORS["dim"])
                else:
                    console.print(f"      {key}={value}", style=COLORS["dim"])

        # Show tool count for active servers
        if is_active:
            tool_count = sum(1 for t in middleware._tools_cache if t.get("server") == name)
            console.print(f"    Tools: {tool_count}", style=COLORS["dim"])

        console.print()

    console.print(
        f"Configuration file: {mcp_config.config_path}",
        style=COLORS["dim"],
    )


def _install(url: str, name: str | None = None, skip_install: bool = False) -> None:
    """Install an MCP server from a URL or package name.

    Supports multiple input formats:
    - npm packages: @modelcontextprotocol/server-filesystem, @playwright/mcp
    - git URLs: https://github.com/user/repo, git+https://github.com/user/repo
    - local paths: ./my-mcp-server, /path/to/server
    - HTTP URLs: https://example.com/mcp (SSE transport)

    Args:
        url: URL or package name to install
        name: Optional custom name for the server
        skip_install: Skip the npm/uv add step (just configure)
    """

    from novacode_cli.mcp.presets import get_preset

    console.print()
    console.print("[bold]Installing MCP Server[/bold]", style=COLORS["primary"])
    console.print()

    # Determine the server name from URL or use provided name
    server_name = name or _derive_server_name(url)

    # Check if this matches a known preset
    preset = get_preset(server_name)
    if preset:
        console.print(f"Found matching preset: {preset['name']}", style=COLORS["primary"])
        # Install via preset
        _install_preset(server_name, preset, skip_install)
        return

    # Parse the URL to determine installation method
    url_lower = url.lower().strip()

    # Check for HTTP(S) endpoint
    if url_lower.startswith("http://") or url_lower.startswith("https://"):
        # Check if it's an MCP server endpoint (typically /mcp, /sse, etc.)
        if _is_mcp_http_endpoint(url):
            console.print("Detected: HTTP MCP endpoint", style=COLORS["primary"])
            _install_http_server(server_name, url)
            return
        # Check if it's a git URL
        elif (
            "github.com" in url_lower
            or "gitlab.com" in url_lower
            or url_lower.endswith(".git")
        ):
            console.print("Detected: Git repository", style=COLORS["primary"])
            _install_git_repo(url, server_name, skip_install)
            return

    # Check for npm package (@scope/name or just name)
    if url.startswith("@") or _is_likely_npm_package(url):
        console.print("Detected: npm package", style=COLORS["primary"])
        _install_npm_package(url, server_name, skip_install)
        return

    # Check for local path
    if url.startswith("./") or url.startswith("../") or url.startswith("/") or (
        len(url) > 1 and url[1] == ":"
    ):
        console.print("Detected: local path", style=COLORS["primary"])
        _install_local_path(url, server_name)
        return

    # Fallback: treat as npm package
    console.print("Detected: npm package (fallback)", style=COLORS["primary"])
    _install_npm_package(url, server_name, skip_install)


def _derive_server_name(url: str) -> str:
    """Derive a server name from URL or package name.

    Args:
        url: URL or package name

    Returns:
        A sanitized server name
    """
    import re

    # Extract the package name or repo name
    name = url.strip()

    # Handle git URLs
    if ".git" in name:
        name = name.split(".git")[0]
    if "github.com/" in name.lower():
        name = name.lower().split("github.com/")[-1]
    if "gitlab.com/" in name.lower():
        name = name.lower().split("gitlab.com/")[-1]
    if "git+" in name.lower():
        name = name.lower().split("git+")[-1]

    # Handle npm packages
    if name.startswith("@"):
        name = name[1:].replace("/", "-")

    # Remove any remaining URL parts
    name = name.split("/")[-1]

    # Replace non-alphanumeric with hyphens and lowercase
    name = re.sub(r"[^a-z0-9-]", "-", name.lower())
    name = re.sub(r"-+", "-", name)  # Collapse multiple hyphens
    name = name.strip("-")  # Remove leading/trailing hyphens

    return name or "mcp-server"


def _is_mcp_http_endpoint(url: str) -> bool:
    """Check if URL looks like an MCP HTTP/SSE endpoint.

    Args:
        url: URL to check

    Returns:
        True if URL appears to be an MCP HTTP endpoint
    """
    url_lower = url.lower()
    # Common MCP endpoint paths
    mcp_paths = ["/mcp", "/sse", "/sse/", "/stream", "/events", "/mcp/"]
    return any(url_lower.endswith(path) for path in mcp_paths)


def _is_likely_npm_package(name: str) -> bool:
    """Check if name looks like an npm package.

    Args:
        name: Package name to check

    Returns:
        True if name appears to be an npm package
    """
    import re

    # npm package name pattern: @scope/name or just name
    # Must be lowercase, can contain hyphens, no spaces
    pattern = r"^@?[a-z0-9][-a-z0-9_]*$"
    return bool(re.match(pattern, name))


def _install_http_server(name: str, url: str) -> None:
    """Install an HTTP-based MCP server.

    Args:
        name: Server name
        url: MCP server URL
    """
    config = MCPServerConfig(
        transport="http",
        url=url,
        description=f"MCP server at {url}",
    )

    mcp_config = MCPConfig()
    mcp_config.add_server(name, config)

    console.print()
    console.print(f"✓ MCP server '{name}' configured successfully!", style=COLORS["primary"])
    console.print(f"   URL: {url}", style=COLORS["dim"])
    console.print(f"   Transport: HTTP", style=COLORS["dim"])
    console.print()
    console.print("[dim]Restart your session for changes to take effect.[/dim]")


def _install_git_repo(url: str, name: str, skip_install: bool) -> None:
    """Install MCP server from a git repository.

    Args:
        url: Git repository URL
        name: Server name
        skip_install: Skip the install step
    """
    import shutil

    console.print(f"   URL: {url}", style=COLORS["dim"])
    console.print()

    # Clean up git+ prefix if present
    clean_url = url.replace("git+", "").replace("git://", "https://").rstrip(".git")

    # Determine package manager by checking for package.json
    package_json_url = _check_file_in_repo(clean_url, "package.json")

    if package_json_url:
        # npm/Node.js package
        console.print("Detected: Node.js package", style=COLORS["primary"])
        _install_npm_package(package_json_url, name, skip_install)
    else:
        # Try uvx for Python packages
        uvx_path = shutil.which("uvx")
        if uvx_path:
            console.print("Installing as Python package with uvx...", style=COLORS["primary"])
            config = MCPServerConfig(
                transport="stdio",
                command="uvx",
                args=["--from", clean_url, _derive_server_name(clean_url)],
                env={},
                description=f"MCP server from {clean_url}",
            )
            mcp_config = MCPConfig()
            mcp_config.add_server(name, config)

            console.print()
            console.print(f"✓ MCP server '{name}' configured!", style=COLORS["primary"])
            console.print(f"   Command: uvx --from {clean_url} {name}", style=COLORS["dim"])
            console.print()
            console.print("[dim]Restart your session for changes to take effect.[/dim]")
        else:
            console.print(
                "[yellow]Could not determine package type. "
                "Try specifying manually with /mcp add.[/yellow]"
            )


def _check_file_in_repo(base_url: str, filename: str) -> str | None:
    """Check if a file exists in a git repository.

    Args:
        base_url: Base repository URL
        filename: File to look for

    Returns:
        The filename if found, None otherwise
    """
    # For GitHub, we can check the default branch
    if "github.com" in base_url.lower():
        # Try to get the default branch from GitHub API
        try:
            import json

            api_url = base_url.replace("github.com", "api.github.com", 1) + "/branches"
            req = urllib.request.Request(
                api_url, headers={"Accept": "application/vnd.github.v3+json"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                branches = json.loads(response.read())
                if branches:
                    branch = branches[0]["name"]
                    # Check for package.json
                    repo_path = base_url.split("github.com/")[-1]
                    raw_url = f"https://raw.githubusercontent.com/{repo_path}/{branch}/{filename}"
                    req = urllib.request.Request(raw_url)
                    try:
                        with urllib.request.urlopen(req, timeout=5):
                            return filename
                    except Exception:
                        return None
        except Exception:
            pass
    return None


def _install_npm_package(package: str, name: str, skip_install: bool) -> None:
    """Install MCP server from an npm package.

    Args:
        package: npm package name or URL
        name: Server name
        skip_install: Skip the npm install step
    """
    import shutil
    import subprocess

    npm_path = shutil.which("npm")
    npx_path = shutil.which("npx")

    if not npm_path or not npx_path:
        console.print(
            "[yellow]npm is not installed. "
            "Please install Node.js to use npm packages.[/yellow]"
        )
        return

    console.print(f"   Package: {package}", style=COLORS["dim"])
    console.print()

    if not skip_install:
        console.print("Installing npm package...", style=COLORS["primary"])

        try:
            result = subprocess.run(
                [npm_path, "install", "-g", package],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                console.print(
                    f"[yellow]Warning: npm install had issues: {result.stderr}[/yellow]"
                )
                console.print("[dim]Continuing with npx anyway...[/dim]")
        except subprocess.TimeoutExpired:
            console.print(
                "[yellow]npm install timed out. Continuing with npx anyway...[/yellow]"
            )
        except Exception as e:
            console.print(f"[yellow]Warning: {e}. Continuing with npx anyway...[/yellow]")

    # Determine command and args based on package
    if package.startswith("@"):
        # Scoped package - use npx directly with full package name
        command = "npx"
        args = ["-y", package]
    else:
        command = "npx"
        args = ["-y", package]

    config = MCPServerConfig(
        transport="stdio",
        command=command,
        args=args,
        env={},
        description=f"MCP server from npm package {package}",
    )

    mcp_config = MCPConfig()
    mcp_config.add_server(name, config)

    console.print()
    console.print(
        f"✓ MCP server '{name}' configured successfully!", style=COLORS["primary"]
    )
    console.print(f"   Command: {command}", style=COLORS["dim"])
    console.print(f"   Args: {' '.join(args)}", style=COLORS["dim"])
    console.print()
    console.print("[dim]Restart your session for changes to take effect.[/dim]")


def _install_local_path(path: str, name: str) -> None:
    """Install MCP server from a local path.

    Args:
        path: Local directory path
        name: Server name
    """

    # Resolve to absolute path
    abs_path = str(Path(path).resolve())

    console.print(f"   Path: {abs_path}", style=COLORS["dim"])
    console.print()

    # Check for package.json or pyproject.toml
    package_json = Path(abs_path) / "package.json"
    pyproject = Path(abs_path) / "pyproject.toml"

    if package_json.exists():
        # Node.js package
        console.print("Detected: Node.js package", style=COLORS["primary"])
        config = MCPServerConfig(
            transport="stdio",
            command="node",
            args=["-e", f"require('{abs_path}')"],
            env={},
            description=f"MCP server from local path {abs_path}",
        )
    elif pyproject.exists():
        # Python package
        console.print("Detected: Python package", style=COLORS["primary"])
        # Try to find the module name
        module_name = name.replace("-", "_")
        config = MCPServerConfig(
            transport="stdio",
            command="python",
            args=["-m", module_name],
            env={},
            description=f"MCP server from local path {abs_path}",
        )
    else:
        console.print(
            "[yellow]Could not detect package type. Defaulting to node.[/yellow]"
        )
        config = MCPServerConfig(
            transport="stdio",
            command="node",
            args=["-e", f"require('{abs_path}')"],
            env={},
            description=f"MCP server from local path {abs_path}",
        )

    mcp_config = MCPConfig()
    mcp_config.add_server(name, config)

    console.print()
    console.print(
        f"✓ MCP server '{name}' configured successfully!", style=COLORS["primary"]
    )
    console.print(f"   Path: {abs_path}", style=COLORS["dim"])
    console.print()
    console.print("[dim]Restart your session for changes to take effect.[/dim]")


def _install_preset(preset_id: str, preset: dict[str, Any], skip_install: bool) -> None:
    """Install MCP server using a preset.

    Args:
        preset_id: Preset identifier
        preset: Preset configuration
        skip_install: Skip package installation
    """
    from prompt_toolkit import PromptSession

    console.print(f"   Package: {preset.get('package', 'N/A')}", style=COLORS["dim"])
    console.print()

    # Check if preset needs user input
    user_inputs = {}
    session = PromptSession()

    if "setup_prompt" in preset:
        value = session.prompt(f"{preset['setup_prompt']} ").strip()
        user_inputs[preset["setup_key"]] = value

    if "setup_secondary_prompt" in preset:
        value = session.prompt(f"{preset['setup_secondary_prompt']} ").strip()
        user_inputs[preset["setup_secondary_key"]] = value

    # Install the package if needed
    package = preset.get("package", "")
    if package and not skip_install:
        _auto_install_package(package)

    # Create config from preset
    from novacode_cli.mcp.presets import create_config_from_preset

    config = create_config_from_preset(preset_id, user_inputs)

    if config:
        mcp_config = MCPConfig()
        mcp_config.add_server(preset_id, config)

        console.print()
        console.print(
            f"✓ MCP preset '{preset['name']}' installed successfully!",
            style=COLORS["primary"],
        )
        console.print()
        console.print("[dim]Restart your session for changes to take effect.[/dim]")


def _auto_install_package(package: str) -> None:
    """Automatically install a package using appropriate package manager.

    Args:
        package: Package to install
    """
    import shutil
    import subprocess

    # Check for common prefixes
    if package.startswith("@"):
        # npm scoped package
        npm_path = shutil.which("npm")
        if npm_path:
            console.print(f"Installing {package} with npm...", style=COLORS["primary"])
            try:
                subprocess.run(
                    [npm_path, "install", "-g", package],
                    capture_output=True,
                    timeout=120,
                )
            except Exception as e:
                console.print(f"[yellow]Warning: {e}[/yellow]")
    elif "pip" in package or "uv" in package:
        # Python package
        uv_path = shutil.which("uv")
        pip_path = shutil.which("pip")
        cmd = uv_path or pip_path
        if cmd:
            console.print(f"Installing {package} with pip...", style=COLORS["primary"])
            try:
                subprocess.run([cmd, "install", package], capture_output=True, timeout=120)
            except Exception as e:
                console.print(f"[yellow]Warning: {e}[/yellow]")




async def execute_bash_command_async(command: str) -> None:
    """Execute a bash command and return output as a string.

    Args:
        command: The bash command to execute

    Returns:
        Output string
    """
    import subprocess

    try:
        # Execute the command
        result = subprocess.run(
            command,
            check=False,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=Path.cwd(),
        )

        output_lines = []

        # Show the command
        output_lines.append(f"$ {command}")
        output_lines.append("")

        # Add output
        if result.stdout:
            output_lines.append(result.stdout.strip())
        if result.stderr:
            output_lines.append(f"[stderr]\n{result.stderr.strip()}")

        # Show return code if non-zero
        if result.returncode != 0:
            output_lines.append(f"\nExit code: {result.returncode}")

        return "\n".join(output_lines)  # type: ignore

    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds"  # type: ignore
    except Exception as e:
        return f"Error executing command: {e}"  # type: ignore


def execute_mcp_command(args: argparse.Namespace) -> None:
    """Execute MCP subcommands based on parsed arguments.

    Args:
        args: Parsed command line arguments with mcp_command attribute
    """
    if args.mcp_command == "add":
        # Parse environment variables
        env = {}
        if args.env:
            for env_var in args.env:
                if "=" not in env_var:
                    console.print(
                        f"[bold red]Error:[/bold red] Invalid environment variable format: {env_var}",
                    )
                    console.print(
                        "[dim]Use KEY=VALUE format (e.g., --env ROOT_DIR=/workspace)[/dim]",
                        style=COLORS["dim"],
                    )
                    sys.exit(1)
                key, value = env_var.split("=", 1)
                env[key] = value

        _add(
            name=args.name,
            transport=args.transport,
            url=args.url,
            command=args.command,
            args=args.args,
            env=env,
            description=args.description,
        )

    elif args.mcp_command == "remove":
        _remove(args.name)

    elif args.mcp_command == "enable":
        _enable(args.name)

    elif args.mcp_command == "disable":
        _disable(args.name)

    elif args.mcp_command == "list":
        _list()

    elif args.mcp_command == "install":
        _install(args.url, args.name)

    else:
        # No subcommand provided, show help
        console.print(
            "[yellow]Please specify an MCP subcommand: add, remove, list, install, enable, or disable[/yellow]",
        )
        console.print("\n[bold]Usage:[/bold]", style=COLORS["primary"])
        console.print("  Nova mcp <command> [options]\n")
        console.print("[bold]Available commands:[/bold]", style=COLORS["primary"])
        console.print("  add       Add or update an MCP server")
        console.print("  remove    Remove an MCP server")
        console.print("  enable    Enable a configured MCP server")
        console.print("  disable   Disable a configured MCP server")
        console.print("  list      List all configured MCP servers")
        console.print("  install   Install an MCP server from URL")
        console.print("\n[bold]Examples:[/bold]", style=COLORS["primary"])
        console.print(
            "  Nova mcp add docs-langchain --transport http --url https://docs.langchain.com/mcp",
        )
        console.print(
            "  Nova mcp add filesystem --transport stdio --command 'python -m mcp_server_filesystem'",
        )
        console.print("  Nova mcp list")
        console.print("  Nova mcp remove docs-langchain")
        console.print("\n[dim]For more help on a specific command:[/dim]", style=COLORS["dim"])
        console.print("  Nova mcp <command> --help", style=COLORS["dim"])


__all__ = [
    "execute_mcp_command",
    "setup_mcp_parser",
]
