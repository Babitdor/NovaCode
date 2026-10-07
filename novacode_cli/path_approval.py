"""Path approval system for controlling access to directories."""

import asyncio
import json
import os
import sys
import warnings
from pathlib import Path

from rich.panel import Panel
from rich.text import Text

from .config.config import console


class PathApprovalManager:
    """Manages approved paths for Nova access."""

    def __init__(self):
        """Initialize the path approval manager."""
        self.config_dir = Path.home() / ".nova"
        self.config_file = self.config_dir / "approved_paths.json"
        self._approved_paths = self._load_approved_paths()
        # Build prefix set for O(1) parent lookup
        self._rebuild_prefix_index()

    def _rebuild_prefix_index(self) -> None:
        """Rebuild the prefix index for fast parent path lookups.
        
        This creates a set of all path prefixes that have recursive approval,
        enabling O(1) lookup instead of O(n) iteration.
        """
        self._recursive_prefixes: set[str] = set()
        for path_str, config in self._approved_paths.items():
            if config.get("recursive", False):
                # Store normalized path with trailing separator for prefix matching
                normalized = str(Path(path_str).resolve())
                self._recursive_prefixes.add(normalized)

    def _check_file_permissions(self) -> bool:
        """Check if config file has secure permissions.

        Returns:
            True if permissions are secure, False otherwise
        """
        if not self.config_file.exists():
            return True

        # Windows does not use Unix permission bits — skip the check entirely.
        if os.name == "nt":
            return True

        try:
            mode = os.stat(self.config_file).st_mode
            # Check if group or others have any permissions
            if mode & 0o077:
                return False
        except OSError:
            pass
        return True

    def _load_approved_paths(self) -> dict:
        """Load approved paths from config file."""
        if not self.config_file.exists():
            return {}

        # Check file permissions
        if not self._check_file_permissions():
            warnings.warn(
                f"{self.config_file} has insecure permissions (group/other readable). "
                "Run 'chmod 600' to fix.",
                UserWarning,
            )

        try:
            with open(self.config_file) as f:
                data = json.load(f)
                # Validate loaded paths
                validated = {}
                for path_str, config in data.items():
                    try:
                        path = Path(path_str)
                        if path.exists():
                            validated[path_str] = config
                    except Exception:
                        # Skip invalid paths
                        continue
                return validated
        except Exception:
            return {}

    def _save_approved_paths(self) -> None:
        """Save approved paths to config file with secure permissions."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w") as f:
            json.dump(self._approved_paths, f, indent=2)

        # Set secure permissions (owner read/write only)
        try:
            os.chmod(self.config_file, 0o600)
        except OSError:
            pass  # Windows doesn't support Unix permissions

    def is_path_approved(self, path: Path) -> bool:
        """Check if a path is approved for access.

        Uses optimized prefix matching for O(1) lookup instead of O(n) iteration.

        Args:
            path: The path to check

        Returns:
            True if the path or any parent is approved, False otherwise
        """
        path = path.resolve()
        path_str = str(path)

        # O(1) exact match check
        if path_str in self._approved_paths:
            return True

        # O(d) parent check where d = path depth (typically < 10)
        # Walk up the directory tree and check each parent
        current = path
        while current != current.parent:  # Stop at root
            current_str = str(current)
            if current_str in self._recursive_prefixes:
                return True
            current = current.parent

        return False

    def approve_path(self, path: Path, recursive: bool = True) -> None:
        """Approve a path for access.

        Args:
            path: The path to approve
            recursive: If True, also approve all subdirectories
        """
        path = path.resolve()
        path_str = str(path)

        self._approved_paths[path_str] = {
            "recursive": recursive,
            "approved_at": Path.cwd().as_posix(),
        }
        self._save_approved_paths()
        self._rebuild_prefix_index()  # Update prefix index

    def revoke_path(self, path: Path) -> bool:
        """Revoke approval for a path.

        Args:
            path: The path to revoke

        Returns:
            True if path was revoked, False if it wasn't approved
        """
        path = path.resolve()
        path_str = str(path)

        if path_str in self._approved_paths:
            del self._approved_paths[path_str]
            self._save_approved_paths()
            self._rebuild_prefix_index()  # Update prefix index
            return True
        return False

    def list_approved_paths(self) -> dict:
        """Get all approved paths.

        Returns:
            Dictionary of approved paths and their configurations
        """
        return self._approved_paths.copy()

    async def prompt_for_approval(self, path: Path) -> bool:
        """Use the native startup screen when attached to an interactive terminal."""
        if sys.stdin.isatty() and sys.stdout.isatty():
            from novacode_cli.tui.workspace_approval import WorkspaceApprovalApp

            choice = await WorkspaceApprovalApp(path).run_async()
            if choice not in {"recursive", "only"}:
                return False
            await asyncio.to_thread(self.approve_path, path, recursive=choice == "recursive")
            return True
        return await self._prompt_for_approval_text(path)

    async def _prompt_for_approval_text(self, path: Path) -> bool:
        """Prompt user to approve a path.

        Args:
            path: The path requesting approval

        Returns:
            True if user approved, False otherwise
        """
        console.print()

        # Create header
        header = Text()
        header.append("🔒 ", style="yellow")
        header.append("Path Access Request", style="bold yellow")

        # Create message
        message_lines = [
            "Nova is requesting access to:",
            "",
            f"  📁 {path}",
            "",
            "This allows Nova to:",
            "  • Read files in this directory",
            "  • Write and modify files",
            "  • Execute commands in this context",
            "",
            "Do you want to grant access?",
        ]

        panel = Panel(
            "\n".join(message_lines),
            title=header,
            border_style="yellow",
            padding=(1, 2),
        )
        console.print(panel)
        console.print()

        # Prompt for approval
        console.print("[bold]Options:[/bold]")
        console.print("  [green]y[/green] - Yes, approve this directory and subdirectories")
        console.print("  [green]o[/green] - Yes, approve only this directory (not subdirectories)")
        console.print("  [red]n[/red] - No, deny access")
        console.print()

        loop = asyncio.get_event_loop()
        while True:
            try:
                choice = await loop.run_in_executor(None, lambda: input("Your choice (y/o/n): "))
                choice = choice.strip().lower()

                if choice in ["y", "yes"]:
                    self.approve_path(path, recursive=True)
                    console.print()
                    console.print("✅ ", style="green", end="")
                    console.print("[green]Access granted (including subdirectories)[/green]")
                    console.print()
                    return True
                if choice in ["o", "only"]:
                    self.approve_path(path, recursive=False)
                    console.print()
                    console.print("✅ ", style="green", end="")
                    console.print("[green]Access granted (this directory only)[/green]")
                    console.print()
                    return True
                if choice in ["n", "no"]:
                    console.print()
                    console.print("❌ ", style="red", end="")
                    console.print("[red]Access denied[/red]")
                    console.print()
                    return False
                console.print("[yellow]Invalid choice. Please enter y, o, or n.[/yellow]")
            except (EOFError, KeyboardInterrupt):
                console.print()
                console.print("❌ ", style="red", end="")
                console.print("[red]Access denied[/red]")
                console.print()
                return False


async def check_path_approval(path: Path | None = None) -> bool:
    """Check if the current path is approved, prompting if needed.

    Args:
        path: The path to check (defaults to current directory)

    Returns:
        True if approved, False otherwise
    """
    if path is None:
        path = Path.cwd()

    manager = PathApprovalManager()

    if manager.is_path_approved(path):
        return True

    return await manager.prompt_for_approval(path)
