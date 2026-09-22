"""UI rendering and display utilities for the CLI.

This module provides rich console-based UI components and formatting functions:

Key Components:
- TokenTracker: Track and display token usage statistics
- show_help(): Display help information for CLI commands
- format_tool_display(): Format tool calls for display
- format_tool_message_content(): Format tool execution results
- render_diff_block(): Render code diffs with syntax highlighting
- render_file_operation(): Display file operations with formatting
- render_todo_list(): Display task lists with progress indicators

Features:
- Rich console output with colors and formatting
- Token usage tracking and context window display
- Tool call visualization with arguments and results
- Code diff rendering with line-by-line comparison
- Todo list rendering with status indicators
- Markdown rendering for rich text content

Color Scheme:
Uses the COLORS constant from config.py for consistent styling:
- primary: Bright red for headings and primary actions
- secondary: Deep red for highlights
- success: Green for success states
- error: Deep red for errors
- warning: Orange for warnings

The UI is built on Rich library for terminal formatting and uses
consistent styling across all components.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any

from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from novacode_cli.context import ContextBreakdown

from ..config.config import COLORS, COMMANDS, MAX_ARG_LENGTH, NOVA_CODE_ASCII, console
from ..file_ops import FileOperationRecord


def truncate_value(value: str, max_length: int = MAX_ARG_LENGTH) -> str:
    """Truncate a string value if it exceeds max_length."""
    if len(value) > max_length:
        return value[:max_length] + "..."
    return value


def format_tool_display(tool_name: str, tool_args: dict) -> str:
    """Format tool calls for display with tool-specific smart formatting.

    Shows the most relevant information for each tool type rather than all arguments.

    Args:
        tool_name: Name of the tool being called
        tool_args: Dictionary of tool arguments

    Returns:
        Formatted string for display (e.g., "read_file(config.py)")

    Examples:
        read_file(path="/long/path/file.py") → "read_file(file.py)"
        web_search(query="how to code", max_results=5) → 'web_search("how to code")'
        shell(command="pip install foo") → 'shell("pip install foo")'
    """

    def abbreviate_path(path_str: str, max_length: int = 60) -> str:
        """Abbreviate a file path intelligently - show basename or relative path."""
        try:
            path = Path(path_str)

            # If it's just a filename (no directory parts), return as-is
            if len(path.parts) == 1:
                return path_str

            # Try to get relative path from current working directory
            try:
                rel_path = path.relative_to(Path.cwd())
                rel_str = str(rel_path)
                # Use relative if it's shorter and not too long
                if len(rel_str) < len(path_str) and len(rel_str) <= max_length:
                    return rel_str
            except (ValueError, Exception):
                pass

            # If absolute path is reasonable length, use it
            if len(path_str) <= max_length:
                return path_str

            # Otherwise, just show basename (filename only)
            return path.name
        except Exception:
            # Fallback to original string if any error
            return truncate_value(path_str, max_length)

    # Tool-specific formatting - show the most important argument(s)
    if tool_name in ("read_file", "write_file", "edit_file", "Read", "Write", "Edit"):
        # File operations: show the primary file path argument (file_path or path)
        path_value = tool_args.get("file_path")
        if path_value is None:
            path_value = tool_args.get("path")
        if path_value is not None:
            path = abbreviate_path(str(path_value))
            return f"{tool_name}({path})"

    elif tool_name == "web_search":
        # Web search: show the query string
        if "query" in tool_args:
            query = str(tool_args["query"])
            query = truncate_value(query, 100)
            return f'{tool_name}("{query}")'

    elif tool_name == "grep":
        # Grep: show the search pattern
        if "pattern" in tool_args:
            pattern = str(tool_args["pattern"])
            pattern = truncate_value(pattern, 70)
            return f'{tool_name}("{pattern}")'

    elif tool_name == "shell":
        # Shell: show the command being executed
        if "command" in tool_args:
            command = str(tool_args["command"])
            command = truncate_value(command, 120)
            return f'{tool_name}("{command}")'

    elif tool_name == "ls":
        # ls: show directory, or empty if current directory
        if tool_args.get("path"):
            path = abbreviate_path(str(tool_args["path"]))
            return f"{tool_name}({path})"
        return f"{tool_name}()"

    elif tool_name == "glob":
        # Glob: show the pattern
        if "pattern" in tool_args:
            pattern = str(tool_args["pattern"])
            pattern = truncate_value(pattern, 80)
            return f'{tool_name}("{pattern}")'

    elif tool_name == "fetch_url":
        # Fetch URL / HTTP: show method, URL, data, auth
        parts = []
        if "method" in tool_args and str(tool_args["method"]).upper() != "GET":
            parts.append(str(tool_args["method"]).upper())
        if "url" in tool_args:
            url = str(tool_args["url"])
            url = truncate_value(url, 80)
            parts.append(url)
        if "data" in tool_args and tool_args["data"]:
            data = str(tool_args["data"])
            data = truncate_value(data, 60)
            parts.append(f"data={data}")
        if "auth" in tool_args:
            parts.append("(auth)")
        if parts:
            return f"{tool_name}({' '.join(parts)})"

    elif tool_name == "task":
        # Task: "[Agent Name] description" — agent name is title-cased, no icon
        subagent_type = tool_args.get("subagent_type", "unknown")
        agent_label = subagent_type.replace("-", " ").replace("_", " ").title()
        if "description" in tool_args:
            desc = str(tool_args["description"])
            desc = truncate_value(desc, 100)
            return f"[{agent_label}] {desc}"
        return f"[{agent_label}]"

    elif tool_name == "write_todos":
        # Todos: show count of items
        if "todos" in tool_args and isinstance(tool_args["todos"], list):
            count = len(tool_args["todos"])
            return f"{tool_name}({count} items)"

    elif tool_name == "duckduckgo_search":
        # DuckDuckGo: show the query string
        if "query" in tool_args:
            query = str(tool_args["query"])
            query = truncate_value(query, 100)
            return f'{tool_name}("{query}")'

    elif tool_name == "docs_search":
        # Docs search: show query and optional topic
        if "query" in tool_args:
            query = str(tool_args["query"])
            query = truncate_value(query, 80)
            topic = tool_args.get("topic", "")
            if topic:
                return f'{tool_name}("{query}", topic={topic})'
            return f'{tool_name}("{query}")'

    elif tool_name in ("git_status", "git_branch", "git_stash"):
        # Git ops with optional repo path
        repo = tool_args.get("repo_path", ".")
        if repo != ".":
            return f"{tool_name}({abbreviate_path(repo)})"
        return f"{tool_name}()"

    elif tool_name == "git_log":
        # Git log: show file_path filter or max_count
        if tool_args.get("file_path"):
            path = abbreviate_path(str(tool_args["file_path"]))
            return f"{tool_name}({path})"
        max_count = tool_args.get("max_count", 10)
        author = tool_args.get("author")
        if author:
            return f"{tool_name}(n={max_count}, author={author})"
        return f"{tool_name}(n={max_count})"

    elif tool_name == "git_diff":
        # Git diff: show file path or commits being compared
        if tool_args.get("file_path"):
            path = abbreviate_path(str(tool_args["file_path"]))
            return f"{tool_name}({path})"
        if tool_args.get("staged"):
            return f"{tool_name}(--staged)"
        c1 = tool_args.get("commit1")
        c2 = tool_args.get("commit2")
        if c1 and c2:
            return f"{tool_name}({c1[:8]}..{c2[:8]})"
        if c1:
            return f"{tool_name}({c1[:8]})"
        return f"{tool_name}()"

    elif tool_name == "git_blame":
        # Git blame: show file path
        if "file_path" in tool_args:
            path = abbreviate_path(str(tool_args["file_path"]))
            return f"{tool_name}({path})"

    elif tool_name == "run_tests":
        # Test runner: show command or test path
        if "command" in tool_args:
            cmd = truncate_value(str(tool_args["command"]), 80)
            return f'{tool_name}("{cmd}")'
        if "test_path" in tool_args:
            path = abbreviate_path(str(tool_args["test_path"]))
            return f"{tool_name}({path})"
        framework = tool_args.get("framework", "")
        if framework:
            return f"{tool_name}({framework})"
        return f"{tool_name}()"

    elif tool_name in ("lint_code", "check_types", "format_code_file"):
        # Code quality: show file path or directory
        for key in ("file_path", "path", "directory"):
            if tool_args.get(key):
                path = abbreviate_path(str(tool_args[key]))
                return f"{tool_name}({path})"
        return f"{tool_name}()"

    elif tool_name.startswith("browser_"):
        # Browser tools: show URL or selector
        for key in ("url", "selector", "query", "text"):
            if tool_args.get(key):
                val = truncate_value(str(tool_args[key]), 80)
                return f'{tool_name}("{val}")'
        return f"{tool_name}()"

    elif tool_name in ("write_memory", "read_memory", "delete_memory"):
        if "key" in tool_args:
            key = truncate_value(str(tool_args["key"]), 60)
            return f'{tool_name}("{key}")'

    elif tool_name == "ask_question":
        return f"{tool_name}()"

    # Fallback: generic formatting for unknown tools
    # Show all arguments in key=value format
    args_str = ", ".join(f"{k}={truncate_value(str(v), 50)}" for k, v in tool_args.items())
    return f"{tool_name}({args_str})"


def format_tool_message_content(content: Any) -> str:
    """Convert ToolMessage content into a printable string."""
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            else:
                try:
                    parts.append(json.dumps(item))
                except Exception:
                    parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def render_tool_panel(
    tool_name: str,
    tool_display: str,
    icon: str = "🔧",
    tool_color: str | None = None,
) -> None:
    """Render a tool call in a beautiful bordered panel with enhanced visuals.

    Responsive to terminal width with beautiful styling:
    - Gradient-inspired colors based on tool type
    - Enhanced visual hierarchy with proper spacing
    - Subtle decorative elements for better aesthetics

    Args:
        tool_name: Name of the tool being called
        tool_display: Formatted display string for the tool
        icon: Icon to show (default: wrench)
        tool_color: Optional color for the border (uses COLORS['tool'] if not provided)
    """
    if tool_color is None:
        tool_color = COLORS.get("tool", "cyan")

    # Get terminal width and calculate responsive panel width
    terminal_width = console.width
    # Use 80% of terminal width, minimum 50, maximum 140
    panel_width = max(50, min(140, int(terminal_width * 0.8)))

    # Calculate available width for content
    available_width = panel_width - 10  # Account for borders, padding, and decorations

    # Truncate with ellipsis if too long
    if len(tool_display) > available_width:
        max_display_len = available_width - 3
        tool_display = tool_display[:max_display_len] + "..."

    # Build beautiful content with visual hierarchy
    # Add subtle separator line for visual appeal
    separator = "─" * (panel_width - 10)

    body_lines = [
        "",  # Top padding
        f"  {icon}  [bold]{tool_display}[/bold]",  # Main content with icon spacing
        "",  # Bottom padding
    ]

    # Create beautiful title with decorative elements
    title_text = f"[bold {tool_color}]◆[/bold {tool_color}] [bold]Tool Call[/bold] [bold {tool_color}]◆[/bold {tool_color}]"

    console.print()
    console.print(
        Panel(
            "\n".join(body_lines),
            title=title_text,
            border_style=tool_color,
            box=box.DOUBLE,  # Use double border for more elegance
            padding=(0, 2),  # Tighter vertical padding
            width=panel_width,
            expand=False,  # Don't expand beyond calculated width
        )
    )
    console.print()


def format_tool_result_preview(
    tool_name: str,
    content: str,
    status: str,
    elapsed_s: float | None = None,
) -> str | None:
    """Return a one-line result summary for a completed tool call.

    Used to give the user observability into what the agent saw back from
    each tool without dumping the full content.  Returns None for tools
    whose results are already rendered elsewhere (file operations).
    """
    # File ops have their own rich render_file_operation — skip them
    if tool_name in ("read_file", "write_file", "edit_file", "Read", "Write", "Edit"):
        return None

    # Task (subagent) results are handled by completion banners — skip preview
    if tool_name == "task":
        return None

    duration = f" · {elapsed_s:.1f}s" if elapsed_s is not None and elapsed_s >= 1.0 else ""

    content_str = content if isinstance(content, str) else str(content or "")
    is_error = status not in ("success", None) or content_str.lstrip().lower().startswith("error")

    if is_error:
        first_line = content_str.splitlines()[0][:100] if content_str else "error"
        return f"✗ {first_line}{duration}"

    if tool_name in ("execute", "execute_bash", "shell"):
        # Parse "[Command succeeded/failed with exit code N]" suffix
        m = re.search(r"\[Command (?:succeeded|failed) with exit code (\d+)\]", content_str)
        exit_code = int(m.group(1)) if m else None
        output_lines = [
            l
            for l in content_str.splitlines()
            if l.strip()
            and not l.startswith("[Command")
            and not l.startswith("[Output was truncated")
        ]
        line_count = len(output_lines)
        if exit_code is not None:
            if exit_code == 0:
                return f"exit 0 · {line_count} line{'s' if line_count != 1 else ''}{duration}"
            snippet = output_lines[0][:80] if output_lines else ""
            return (
                f"✗ exit {exit_code} · {snippet}{duration}"
                if snippet
                else f"✗ exit {exit_code}{duration}"
            )
        return f"{line_count} line{'s' if line_count != 1 else ''}{duration}"

    if tool_name == "grep":
        if (
            not content_str
            or "no matches" in content_str.lower()
            or content_str.strip() in ("[]", "")
        ):
            return f"no matches{duration}"
        lines = [l for l in content_str.splitlines() if l.strip()]
        count = len(lines)
        return f"{count} match{'es' if count != 1 else ''}{duration}"

    if tool_name in ("ls", "glob"):
        if not content_str or content_str.strip() in ("[]", ""):
            return f"0 items{duration}"
        try:
            import ast

            items = ast.literal_eval(content_str)
            if isinstance(items, list):
                count = len(items)
                return f"{count} item{'s' if count != 1 else ''}{duration}"
        except Exception:
            pass
        lines = [l for l in content_str.splitlines() if l.strip()]
        return f"{len(lines)} item{'s' if len(lines) != 1 else ''}{duration}"

    if tool_name in ("web_search", "duckduckgo_search", "docs_search"):
        try:
            data = json.loads(content_str)
            if isinstance(data, list):
                count = len(data)
                return f"{count} result{'s' if count != 1 else ''}{duration}"
        except Exception:
            pass
        lines = [l for l in content_str.splitlines() if l.strip()]
        return f"{len(lines)} results{duration}"

    if tool_name in ("fetch_url",):
        size = len(content_str.encode("utf-8"))
        size_str = f"{size // 1024}KB" if size >= 1024 else f"{size}B"
        return f"{size_str}{duration}"

    if tool_name in ("git_status", "git_log", "git_diff", "git_blame", "git_branch", "git_stash"):
        lines = [l for l in content_str.splitlines() if l.strip()]
        if not lines:
            return f"no output{duration}"
        return f"{len(lines)} line{'s' if len(lines) != 1 else ''}{duration}"

    if tool_name in ("run_tests",):
        # Surface pass/fail from test output
        lower = content_str.lower()
        if "passed" in lower or "ok" in lower:
            # Try to extract e.g. "5 passed"
            m = re.search(r"(\d+) passed", lower)
            if m:
                return f"✓ {m.group(1)} passed{duration}"
            return f"✓ passed{duration}"
        if "failed" in lower or "error" in lower:
            lines = [l for l in content_str.splitlines() if l.strip()]
            first = lines[0][:80] if lines else "test failed"
            return f"✗ {first}{duration}"
        lines = [l for l in content_str.splitlines() if l.strip()]
        return f"{len(lines)} line{'s' if len(lines) != 1 else ''}{duration}"

    # Generic: ✓ with duration only if something took notable time
    return f"✓{duration}"


class TokenTracker:
    """Track token usage across the conversation.

    Enhanced to support detailed context breakdown and visual display
    for the /context command.
    """

    def __init__(self) -> None:
        self.baseline_context = 0  # Baseline system context (system + agent.md + tools)
        self.current_context = 0  # Total context including messages (from API input_tokens)
        self.last_output = 0

        # Cumulative session usage: input+output summed over every turn. Unlike
        # current_context (bounded by the window, drops on compaction), this only
        # ever climbs — it's the "how much have I used this session" number.
        self.session_total_tokens = 0
        # Budget the session meter is a percentage of. Configurable; 1M default.
        self.session_token_budget = 1_000_000

        # Prompt-caching breakdown (Anthropic only, 0 for other providers)
        self.last_cache_read = 0  # tokens read from prompt cache this turn
        self.last_cache_creation = 0  # tokens written to prompt cache this turn

        # Whether we've received at least one real API response
        self.has_api_data = False

        # Model information for context window calculation
        self.model_name: str | None = None
        self.context_window_size: int = 128_000

        # Message counts for detailed breakdown
        self.user_message_count = 0
        self.assistant_message_count = 0
        self.tool_call_count = 0

        # Detailed breakdown from post-turn analysis
        self._last_breakdown: ContextBreakdown | None = None

    @property
    def session_pct(self) -> float:
        """Session usage as a percentage of the configured budget."""
        if not self.session_token_budget:
            return 0.0
        return self.session_total_tokens / self.session_token_budget * 100

    def set_model(self, model_name: str) -> None:
        """Set the model name for context window calculation.

        Args:
            model_name: The name of the model being used
        """
        from novacode_cli.context import ContextManager

        self.model_name = model_name
        self.context_window_size = ContextManager(model_name).window_size()

    def set_baseline(self, tokens: int) -> None:
        """Set the baseline context token count.

        Args:
            tokens: The baseline token count (system prompt + agent.md + tools)
        """
        self.baseline_context = tokens
        self.current_context = tokens

    def reset(self, *, reset_session: bool = False) -> None:
        """Reset to baseline (for /clear and /compact commands).

        By default session_total_tokens is NOT reset — the session usage meter
        must survive compaction and resume (it tracks cumulative usage, not
        context). Pass reset_session=True for a true fresh start (/clear), which
        zeroes the cumulative session meter too.
        """
        self.current_context = self.baseline_context
        self.last_output = 0
        self.last_cache_read = 0
        self.last_cache_creation = 0
        self.has_api_data = False
        self.user_message_count = 0
        self.assistant_message_count = 0
        self.tool_call_count = 0
        self._last_breakdown = None
        if reset_session:
            self.session_total_tokens = 0

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """Add tokens from an API response.

        Args:
            input_tokens: Total context sent to the model this turn
                (already includes cache_read + cache_creation for accurate window accounting).
            output_tokens: Tokens generated by the model.
            cache_read_tokens: Tokens served from the prompt cache (Anthropic only).
            cache_creation_tokens: Tokens written to the prompt cache (Anthropic only).
        """
        self.current_context = input_tokens
        self.last_output = output_tokens
        self.last_cache_read = cache_read_tokens
        self.last_cache_creation = cache_creation_tokens
        self.has_api_data = True
        # add() is called once per finalized turn (agent_loop yields the captured
        # usage once), so summing here accumulates real per-turn usage.
        self.session_total_tokens += input_tokens + output_tokens

    def increment_user_messages(self) -> None:
        """Increment user message count."""
        self.user_message_count += 1

    def increment_assistant_messages(self) -> None:
        """Increment assistant message count."""
        self.assistant_message_count += 1

    def increment_tool_calls(self, count: int = 1) -> None:
        """Increment tool call count."""
        self.tool_call_count += count

    def set_breakdown(self, breakdown: ContextBreakdown) -> None:
        """Set a detailed breakdown from post-turn analysis.

        Merges API-sourced totals (more accurate) with message-level detail.

        Args:
            breakdown: ContextBreakdown built from agent state messages.
        """
        self._last_breakdown = breakdown
        # Update message counts from the detailed breakdown
        self.user_message_count = breakdown.user_message_count
        self.assistant_message_count = breakdown.assistant_message_count
        self.tool_call_count = breakdown.tool_call_count

    def get_breakdown(self) -> ContextBreakdown:
        """Get detailed context breakdown.

        If a detailed breakdown was set via set_breakdown(), returns it
        with API-sourced total_tokens (more accurate than char estimation).

        Returns:
            ContextBreakdown with current usage statistics
        """
        from novacode_cli.context import ContextBreakdown

        if hasattr(self, "_last_breakdown") and self._last_breakdown is not None:
            bd = self._last_breakdown
            # Override total_tokens with API data if available (more accurate).
            if self.has_api_data and self.current_context > 0:
                bd.total_tokens = self.current_context
                # Keep bd.context_window_size: it was detected live this turn
                # (build_context_breakdown, use_dynamic=True), so it reflects the
                # real Ollama-allocated window once the model has loaded — unlike
                # self.context_window_size, captured once at set_model() time.
                # Refresh the stored copy so direct readers stay current too.
                if bd.context_window_size:
                    self.context_window_size = bd.context_window_size
            return bd

        return ContextBreakdown(
            system_prompt_tokens=self.baseline_context,
            total_tokens=self.current_context,
            context_window_size=self.context_window_size,
            user_message_count=self.user_message_count,
            assistant_message_count=self.assistant_message_count,
            tool_call_count=self.tool_call_count,
        )

    def display_last(self) -> None:
        """Display current context size after this turn."""
        if self.last_output and self.last_output >= 1000:
            console.print(f"  Generated: {self.last_output:,} tokens", style="dim")
        if self.current_context:
            console.print(f"  Current context: {self.current_context:,} tokens", style="dim")

    def display_session(self) -> None:
        """Display current context size with color-coded percentage."""
        from novacode_cli.context import (
            CONTEXT_CRITICAL_THRESHOLD,
            CONTEXT_WARNING_THRESHOLD,
        )

        console.print("\n[bold]Token Usage:[/bold]", style=COLORS["primary"])

        # Check if we've had any actual API calls yet (current > baseline means we have conversation)
        has_conversation = self.current_context > self.baseline_context

        if self.baseline_context > 0:
            console.print(
                f"  Baseline: {self.baseline_context:,} tokens [dim](system + agent.md)[/dim]",
                style=COLORS["dim"],
            )

            if not has_conversation:
                # Before first message - warn that tools aren't counted yet
                console.print(
                    "  [dim]Note: Tool definitions (~5k tokens) included after first message[/dim]"
                )

        if has_conversation:
            tools_and_conversation = self.current_context - self.baseline_context
            console.print(
                f"  Tools + conversation: {tools_and_conversation:,} tokens",
                style=COLORS["dim"],
            )

        # Color-code the total based on context window usage
        total_str = f"{self.current_context:,}"
        if self.context_window_size > 0:
            usage_pct = (self.current_context / self.context_window_size) * 100
            if usage_pct >= CONTEXT_CRITICAL_THRESHOLD * 100:
                pct_color = COLORS["error"]
            elif usage_pct >= CONTEXT_WARNING_THRESHOLD * 100:
                pct_color = COLORS["warning"]
            else:
                pct_color = COLORS["success"]
            console.print(
                f"  Total: {total_str} tokens [{pct_color}]{usage_pct:.1f}%[/{pct_color}] of {self.context_window_size:,}",
                style="bold " + COLORS["dim"],
            )
        else:
            console.print(f"  Total: {total_str} tokens", style="bold " + COLORS["dim"])

        console.print()

    def display_context(self) -> None:
        """Display detailed context window usage with visual progress bar.

        This is the handler for the /context command.
        """
        from rich import box
        from rich.table import Table

        from novacode_cli.context import (
            CONTEXT_CRITICAL_THRESHOLD,
            CONTEXT_WARNING_THRESHOLD,
        )

        breakdown = self.get_breakdown()
        window = breakdown.context_window_size

        console.print()
        console.print("[bold]Context Window Usage[/bold]", style=COLORS["primary"])

        # Data source label
        if not self.has_api_data:
            console.print("  [dim](estimated — will update after first API response)[/dim]")
        console.print()

        # Progress bar
        usage_pct = breakdown.usage_percentage
        if usage_pct < CONTEXT_WARNING_THRESHOLD * 100:
            bar_color = COLORS["success"]
        elif usage_pct < CONTEXT_CRITICAL_THRESHOLD * 100:
            bar_color = COLORS["warning"]
        else:
            bar_color = COLORS["error"]

        bar_width = 50
        filled = int(bar_width * usage_pct / 100)
        empty = bar_width - filled
        bar = f"[{bar_color}]{'━' * filled}[/{bar_color}][dim]{'─' * empty}[/dim]"
        console.print(f"  {bar} [bold {bar_color}]{usage_pct:.1f}%[/bold {bar_color}]")
        console.print()

        # Token breakdown table
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column("Category", style="dim")
        table.add_column("Tokens", justify="right")
        table.add_column("Percent", justify="right", style="dim")

        def _pct(n: int) -> str:
            return f"{n / window * 100:.1f}%" if window else "—"

        if self.has_api_data:
            # ── Context sent to API this turn ──────────────────────────────
            fresh = self.current_context - self.last_cache_read - self.last_cache_creation
            fresh = max(0, fresh)

            if self.last_cache_read or self.last_cache_creation:
                # Show caching breakdown
                table.add_row(
                    "Context (fresh tokens)",
                    f"{fresh:,}",
                    _pct(fresh),
                )
                if self.last_cache_read:
                    table.add_row(
                        "  ↳ from cache (read)",
                        f"{self.last_cache_read:,}",
                        _pct(self.last_cache_read),
                    )
                if self.last_cache_creation:
                    table.add_row(
                        "  ↳ written to cache",
                        f"{self.last_cache_creation:,}",
                        _pct(self.last_cache_creation),
                    )
            else:
                table.add_row(
                    "Context sent",
                    f"{self.current_context:,}",
                    _pct(self.current_context),
                )

            if self.last_output:
                table.add_row(
                    "Generated (last turn)",
                    f"{self.last_output:,}",
                    _pct(self.last_output),
                )
        else:
            # Pre-first-call: show estimate with caveat
            baseline_pct = (breakdown.baseline_tokens / window * 100) if window else 0
            table.add_row(
                "Baseline (system + memory) [dim]~est.[/dim]",
                f"{breakdown.baseline_tokens:,}",
                f"{baseline_pct:.1f}%",
            )
            table.add_row(
                "[dim]Tools (~est. +5k not yet counted)[/dim]",
                "~5,000",
                _pct(5000),
            )

        table.add_section()

        # Total / remaining — color-code the percentage
        total_pct_style = f"bold {bar_color}"
        table.add_row(
            "[bold]Total context[/bold]",
            f"[bold]{breakdown.total_tokens:,}[/bold]",
            f"[{total_pct_style}]{usage_pct:.1f}%[/{total_pct_style}]",
        )
        remaining_pct = 100 - usage_pct
        table.add_row(
            "Remaining",
            f"{breakdown.remaining_tokens:,}",
            f"{remaining_pct:.1f}%",
        )

        console.print(table)
        console.print()

        # Message counts
        console.print(
            f"  [dim]Messages: {breakdown.user_message_count} user · "
            f"{breakdown.assistant_message_count} assistant · "
            f"{breakdown.tool_call_count} tool calls[/dim]"
        )
        console.print()

        # Context window info
        model_display = self.model_name or "unknown model"
        console.print(f"  [dim]Window: {window:,} tokens ({model_display})[/dim]")
        console.print()

        # Threshold warnings
        if breakdown.is_critical:
            console.print(
                "  [bold red]⚠ Critical: Context nearly full! "
                "Use /compact to summarize conversation.[/bold red]"
            )
        elif breakdown.is_warning:
            console.print(
                "  [yellow]⚠ Warning: Context usage is high. Consider using /compact soon.[/yellow]"
            )
        else:
            console.print(
                f"  [dim green]✓ Context usage healthy ({usage_pct:.0f}%). "
                f"Use /context anytime to check.[/dim green]"
            )
        console.print()


class StreamingOutputRenderer:
    """Render streaming output from processes in real-time.

    Provides buffered output handling with optional line limits and styling.
    Useful for displaying test output, server logs, and other streaming content.
    """

    def __init__(
        self,
        max_lines: int = 1000,
        prefix: str = "",
        style: str = "dim",
    ) -> None:
        """Initialize the streaming renderer.

        Args:
            max_lines: Maximum lines to display (older lines discarded)
            prefix: Prefix to add to each line
            style: Rich style for output lines
        """
        self.max_lines = max_lines
        self.prefix = prefix
        self.style = style
        self._line_count = 0
        self._buffer = ""
        self._truncated = False

    def write(self, data: str) -> None:
        """Write data to the renderer, handling partial lines.

        Args:
            data: String data to write (may contain partial lines)
        """
        if not data:
            return

        # Add to buffer
        self._buffer += data

        # Process complete lines
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._write_line(line)

    def _write_line(self, line: str) -> None:
        """Write a complete line to output.

        Args:
            line: Complete line to write
        """
        if self._line_count >= self.max_lines:
            if not self._truncated:
                console.print(f"[yellow]... output truncated (max {self.max_lines} lines)[/yellow]")
                self._truncated = True
            return

        # Escape Rich markup in the line to prevent formatting issues
        escaped_line = escape(line)
        if self.prefix:
            console.print(f"[{self.style}]{self.prefix}{escaped_line}[/{self.style}]")
        else:
            console.print(f"[{self.style}]{escaped_line}[/{self.style}]")

        self._line_count += 1

    def flush(self) -> None:
        """Flush any remaining buffer content."""
        if self._buffer:
            self._write_line(self._buffer)
            self._buffer = ""

    @property
    def line_count(self) -> int:
        """Get the number of lines written."""
        return self._line_count

    @property
    def was_truncated(self) -> bool:
        """Check if output was truncated."""
        return self._truncated


def _todo_line(todo: dict, indent: str = "") -> str:
    """Format a single todo item as a styled line."""
    status = todo.get("status", "pending")
    content = todo.get("content", "")
    task_id = todo.get("id", "")
    deps = todo.get("depends_on", [])

    if status == "completed":
        icon = "✓"
        style = COLORS["success"]
    elif status == "in_progress":
        icon = "►"
        style = COLORS["primary"]
    elif status == "blocked":
        icon = "🔒"
        style = "red"
    else:  # pending
        icon = "○"
        style = COLORS["dim"]

    id_label = f"[dim]{task_id}.[/dim] " if task_id else ""
    dep_label = f" [dim](← {', '.join(deps)})[/dim]" if deps else ""
    return f"{indent}[{style}]{icon} {id_label}{content}{dep_label}[/{style}]"


def render_todo_list(todos: list[dict], agent_name: str | None = None) -> None:
    """Render todo list as a beautiful rich Panel with checkboxes, supporting subtasks.

    Responsive to terminal width with beautiful styling and visual hierarchy.

    Args:
        todos: List of todo items with content and status
        agent_name: Optional name of the agent for display in the header
    """
    if not todos:
        return

    lines = []
    for todo in todos:
        lines.append(_todo_line(todo))
        for sub in todo.get("subtasks", []):
            lines.append(_todo_line(sub, indent="   "))

    # Get terminal width and calculate responsive panel width
    terminal_width = console.width
    # Use 80% of terminal width, minimum 50, maximum 140
    panel_width = max(50, min(140, int(terminal_width * 0.8)))

    # Create beautiful title with decorative elements and agent name
    if agent_name:
        title_text = f"[bold {COLORS['primary']}]◆[/bold {COLORS['primary']}] [bold]📋 {agent_name}'s Task List[/bold] [bold {COLORS['primary']}]◆[/bold {COLORS['primary']}]"
    else:
        title_text = f"[bold {COLORS['primary']}]◆[/bold {COLORS['primary']}] [bold]📋 Task List[/bold] [bold {COLORS['primary']}]◆[/bold {COLORS['primary']}]"

    panel = Panel(
        "\n".join(lines),
        title=title_text,
        border_style=COLORS["accent"],
        box=box.DOUBLE,
        padding=(0, 1),
        width=panel_width,
        expand=False,
    )
    console.print(panel)


def _format_line_span(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return ""
    if start is not None and end is None:
        return f"(starting at line {start})"
    if start is None and end is not None:
        return f"(through line {end})"
    if start == end:
        return f"(line {start})"
    return f"(lines {start}-{end})"


def render_file_operation(record: FileOperationRecord) -> None:
    """Render a concise summary of a filesystem tool call."""
    label_lookup = {
        "read_file": "Read",
        "write_file": "Write",
        "edit_file": "Update",
    }
    label = label_lookup.get(record.tool_name, record.tool_name)
    header = Text()
    header.append("⏺ ", style=COLORS["tool"])
    header.append(f"{label}({record.display_path})", style=f"bold {COLORS['tool']}")
    console.print(header)

    def _print_detail(message: str, *, style: str = COLORS["dim"]) -> None:
        detail = Text()
        detail.append("  ⎿  ", style=style)
        detail.append(message, style=style)
        console.print(detail)

    if record.status == "error":
        _print_detail(record.error or "Error executing file operation", style="red")
        return

    if record.tool_name == "read_file":
        lines = record.metrics.lines_read
        span = _format_line_span(record.metrics.start_line, record.metrics.end_line)
        detail = f"Read {lines} line{'s' if lines != 1 else ''}"
        if span:
            detail = f"{detail} {span}"
        _print_detail(detail)
    else:
        if record.tool_name == "write_file":
            added = record.metrics.lines_added
            removed = record.metrics.lines_removed
            lines = record.metrics.lines_written
            detail = f"Wrote {lines} line{'s' if lines != 1 else ''}"
            if added or removed:
                detail = f"{detail} (+{added} / -{removed})"
        else:
            added = record.metrics.lines_added
            removed = record.metrics.lines_removed
            detail = f"Edited {record.metrics.lines_written} total line{'s' if record.metrics.lines_written != 1 else ''}"
            if added or removed:
                detail = f"{detail} (+{added} / -{removed})"
        _print_detail(detail)

    # Skip diff display for HIL-approved operations that succeeded
    # (user already saw the diff during approval)
    if record.diff and not (record.hitl_approved and record.status == "success"):
        render_diff(record)


def render_diff(record: FileOperationRecord) -> None:
    """Render diff for a file operation."""
    if not record.diff:
        return
    render_diff_block(record.diff, f"Diff {record.display_path}", path=record.display_path)


def _styled_chunk(spans: list[tuple[str, str]]) -> str:
    """Render ``(text, style)`` spans as Rich markup.

    The caller wraps the result in the diff-marker colour, so syntax token
    styles overlay foreground only and the add/remove signal is preserved.
    """
    out: list[str] = []
    for text, style in spans:
        escaped = escape(text)
        if not escaped:
            continue
        if style:
            out.append(f"[{style}]{escaped}[/{style}]")
        else:
            out.append(escaped)
    return "".join(out)


def _wrap_diff_line(
    code: str,
    marker: str,
    color: str,
    line_num: int | None,
    width: int,
    term_width: int,
    spans: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Wrap long diff lines with proper indentation.

    Args:
        code: Code content to wrap (plain text, used for width measurement)
        marker: Diff marker ('+', '-', ' ')
        color: Base color for the line (diff-marker colour)
        line_num: Line number to display (None for continuation lines)
        width: Width for line number column
        term_width: Terminal width
        spans: Optional ``(text, style)`` syntax spans for ``code``. When
            provided, each wrapped chunk is rendered with per-token styles
            overlaid on ``color``.

    Returns:
        List of formatted lines (may be multiple if wrapped)
    """
    prefix_len = width + 4  # line_num + space + marker + 2 spaces
    available_width = term_width - prefix_len

    def render(chunk: str, chunk_spans: list[tuple[str, str]] | None) -> str:
        if chunk_spans is not None:
            return _styled_chunk(chunk_spans)
        return escape(chunk)

    if len(code) <= available_width:
        body = render(code, spans)
        if line_num is not None:
            return [f"[dim]{line_num:>{width}}[/dim] [{color}]{marker}  {body}[/{color}]"]
        return [f"{' ' * width} [{color}]{marker}  {body}[/{color}]"]

    lines = []
    remaining = code
    remaining_spans = spans
    first = True

    while remaining:
        if len(remaining) <= available_width:
            chunk = remaining
            chunk_spans = remaining_spans
            remaining = ""
            remaining_spans = None
        else:
            # Try to break at a good point (space, comma, etc.)
            chunk = remaining[:available_width]
            # Look for a good break point in the last 20 chars
            break_point = max(
                chunk.rfind(" "),
                chunk.rfind(","),
                chunk.rfind("("),
                chunk.rfind(")"),
            )
            # Break at a good point if one is near the edge, else hard-split.
            split_at = break_point + 1 if break_point > available_width - 20 else available_width
            chunk = remaining[:split_at]
            remaining = remaining[split_at:]
            chunk_spans = _slice_spans(remaining_spans, split_at)
            remaining_spans = _slice_spans(remaining_spans, split_at, drop=True)

        body = render(chunk, chunk_spans)
        if first and line_num is not None:
            lines.append(f"[dim]{line_num:>{width}}[/dim] [{color}]{marker}  {body}[/{color}]")
            first = False
        else:
            lines.append(f"{' ' * width} [{color}]{marker}  {body}[/{color}]")

    return lines


def _slice_spans(
    spans: list[tuple[str, str]] | None,
    n: int,
    *,
    drop: bool = False,
) -> list[tuple[str, str]] | None:
    """Take (or drop) the first ``n`` characters of ``spans``.

    Args:
        spans: ``(text, style)`` spans, or ``None``.
        n: Character count to take (or drop).
        drop: When True, return the remainder after ``n`` characters.

    Returns:
        The sliced spans, or ``None`` if ``spans`` was ``None``.
    """
    if spans is None:
        return None
    out: list[tuple[str, str]] = []
    consumed = 0
    for text, style in spans:
        if consumed >= n:
            if drop:
                out.append((text, style))
            continue
        take = min(len(text), n - consumed)
        if drop:
            if take < len(text):
                out.append((text[take:], style))
        elif take:
            out.append((text[:take], style))
        consumed += take
    return out


def format_diff_rich(diff_lines: list[str], path: str | None = None) -> str:
    """Format diff lines with line numbers and colors.

    Args:
        diff_lines: Diff lines from unified diff
        path: Optional filename/path used to pick a syntax lexer. When the
            extension is unknown, plain diff colouring is used.

    Returns:
        Rich-formatted diff string with line numbers
    """
    if not diff_lines:
        return "[dim]No changes detected[/dim]"

    # Resolve a syntax lexer from the path (None -> no highlighting).
    from .diff_highlight import highlight_line, lexer_for_path

    lexer = lexer_for_path(path)

    # Get terminal width
    term_width = shutil.get_terminal_size().columns

    # Find max line number for width calculation
    max_line = max(
        (
            int(m.group(i))
            for line in diff_lines
            if (m := re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line))
            for i in (1, 2)
        ),
        default=0,
    )
    width = max(3, len(str(max_line)))

    formatted_lines = []
    old_num = new_num = 0

    # Rich colors with backgrounds for better visibility
    # White text on dark backgrounds for additions/deletions
    addition_color = "white on dark_green"
    deletion_color = "white on dark_red"
    context_color = "dim"

    for line in diff_lines:
        if line.strip() == "...":
            formatted_lines.append(f"[{context_color}]...[/{context_color}]")
        elif line.startswith(("---", "+++")):
            continue
        elif m := re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line):
            old_num, new_num = int(m.group(1)), int(m.group(2))
        elif line.startswith("-"):
            code = line[1:]
            formatted_lines.extend(
                _wrap_diff_line(
                    code,
                    "-",
                    deletion_color,
                    old_num,
                    width,
                    term_width,
                    spans=highlight_line(code, lexer),
                )
            )
            old_num += 1
        elif line.startswith("+"):
            code = line[1:]
            formatted_lines.extend(
                _wrap_diff_line(
                    code,
                    "+",
                    addition_color,
                    new_num,
                    width,
                    term_width,
                    spans=highlight_line(code, lexer),
                )
            )
            new_num += 1
        elif line.startswith(" "):
            code = line[1:]
            formatted_lines.extend(
                _wrap_diff_line(
                    code,
                    " ",
                    context_color,
                    old_num,
                    width,
                    term_width,
                    spans=highlight_line(code, lexer),
                )
            )
            old_num += 1
            new_num += 1

    return "\n".join(formatted_lines)


def render_diff_block(diff: str, title: str, path: str | None = None) -> None:
    """Render a diff string with line numbers, colors, and syntax highlighting.

    Args:
        diff: Unified diff text.
        title: Header title for the block.
        path: Optional filename/path used to pick a syntax lexer.
    """
    try:
        # Parse diff into lines and format with line numbers
        diff_lines = diff.splitlines()
        formatted_diff = format_diff_rich(diff_lines, path=path)

        # Print with a simple header
        console.print()
        console.print(f"[bold {COLORS['primary']}]═══ {title} ═══[/bold {COLORS['primary']}]")
        console.print(formatted_diff)
        console.print()
    except (ValueError, AttributeError, IndexError, OSError):
        # Fallback to simple rendering if formatting fails
        console.print()
        console.print(f"[bold {COLORS['primary']}]{title}[/bold {COLORS['primary']}]")
        console.print(diff)
        console.print()


def show_interactive_help() -> None:
    """Show available commands during interactive session."""
    console.print()
    console.print("[bold]Interactive Commands:[/bold]", style=COLORS["primary"])
    console.print()

    for cmd, desc in COMMANDS.items():
        console.print(f"  /{cmd:<12} {desc}", style=COLORS["dim"])

    console.print()
    console.print("[bold]Skill Invocation:[/bold]", style=COLORS["primary"])
    console.print(
        "  /skill:<name>   Invoke a skill directly (e.g., /skill:api-testing, /skill:docker-deploy)",
        style=COLORS["dim"],
    )
    console.print(
        "                  Append arguments after the name (e.g., /skill:code-review src/app.py)",
        style=COLORS["dim"],
    )
    console.print(
        "                  Use /skills list to see all available skills",
        style=COLORS["dim"],
    )

    console.print()
    console.print("[bold]Editing Features:[/bold]", style=COLORS["primary"])
    console.print("  Enter           Submit your message", style=COLORS["dim"])
    console.print(
        "  Alt+Enter       Insert newline (Option+Enter on Mac, or ESC then Enter)",
        style=COLORS["dim"],
    )
    console.print(
        "  Ctrl+E          Open in external editor (nano by default)",
        style=COLORS["dim"],
    )
    console.print("  Ctrl+T          Toggle auto-approve mode", style=COLORS["dim"])
    console.print("  Arrow keys      Navigate input", style=COLORS["dim"])
    console.print(
        "  Ctrl+C          Cancel input or interrupt agent mid-work",
        style=COLORS["dim"],
    )
    console.print()
    console.print("[bold]Special Features:[/bold]", style=COLORS["primary"])
    console.print(
        "  @filename       Type @ to auto-complete files and inject content",
        style=COLORS["dim"],
    )
    console.print("  /command        Type / to see available commands", style=COLORS["dim"])
    console.print(
        "  !command        Type ! to run bash commands (e.g., !ls, !git status)",
        style=COLORS["dim"],
    )
    console.print(
        "                  Completions appear automatically as you type",
        style=COLORS["dim"],
    )
    console.print()
    console.print("[bold]Auto-Approve Mode:[/bold]", style=COLORS["primary"])
    console.print("  Ctrl+T          Toggle auto-approve mode", style=COLORS["dim"])
    console.print(
        "  --auto-approve  Start CLI with auto-approve enabled (via command line)",
        style=COLORS["dim"],
    )
    console.print(
        "  When enabled, tool actions execute without confirmation prompts",
        style=COLORS["dim"],
    )
    console.print()


def show_help() -> None:
    """Show help information."""
    console.print()
    console.print(NOVA_CODE_ASCII, style=f"bold {COLORS['primary']}")
    console.print()

    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  nova [OPTIONS]                           Start interactive session")
    console.print("  nova list                                List all available agents")
    console.print("  nova reset --agent AGENT                 Reset agent to default prompt")
    console.print("  nova reset --agent AGENT --target SOURCE Reset agent to copy of another agent")
    console.print("  nova help                                Show this help message")
    console.print()

    console.print("[bold]Options:[/bold]", style=COLORS["primary"])
    console.print("  --agent NAME                  Agent identifier (default: agent)")
    console.print("  --auto-approve                Auto-approve tool usage without prompting")
    console.print(
        "  --sandbox TYPE                Execution sandbox: os (default Linux/macOS; "
        "Windows defaults to host+approvals), modal, runloop, daytona, docker (Windows-only opt-in)"
    )
    console.print(
        "  --no-sandbox                  Run shell unconfined on the host (no OS/Docker sandbox)"
    )
    console.print("  --sandbox-id ID               Reuse existing sandbox (skips creation/cleanup)")
    console.print()

    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print(
        "  nova                              # Start with default agent",
        style=COLORS["dim"],
    )
    console.print(
        "  nova --agent mybot                # Start with agent named 'mybot'",
        style=COLORS["dim"],
    )
    console.print(
        "  nova --auto-approve               # Start with auto-approve enabled",
        style=COLORS["dim"],
    )
    console.print(
        "  nova --no-sandbox                 # Run shell unconfined on the host",
        style=COLORS["dim"],
    )
    console.print(
        "  nova --sandbox runloop            # Execute code in Runloop sandbox",
        style=COLORS["dim"],
    )
    console.print(
        "  nova --sandbox modal              # Execute code in Modal sandbox",
        style=COLORS["dim"],
    )
    console.print(
        "  nova --sandbox runloop --sandbox-id dbx_123  # Reuse existing sandbox",
        style=COLORS["dim"],
    )
    console.print("  nova list                         # List all agents", style=COLORS["dim"])
    console.print(
        "  nova reset --agent mybot          # Reset mybot to default",
        style=COLORS["dim"],
    )
    console.print(
        "  nova reset --agent mybot --target other # Reset mybot to copy of 'other' agent",
        style=COLORS["dim"],
    )
    console.print()

    console.print("[bold]Long-term Memory:[/bold]", style=COLORS["primary"])
    console.print(
        "  By default, long-term memory is ENABLED using agent name 'nova-agent'.",
        style=COLORS["dim"],
    )
    console.print("  Memory includes:", style=COLORS["dim"])
    console.print("  - Persistent agent.md file with your instructions", style=COLORS["dim"])
    console.print("  - /memories/ folder for storing context across sessions", style=COLORS["dim"])
    console.print()

    console.print("[bold]Agent Storage:[/bold]", style=COLORS["primary"])
    console.print("  Agents are stored in: ~/.nova/AGENT_NAME/", style=COLORS["dim"])
    console.print("  Each agent has an agent.md file containing its prompt", style=COLORS["dim"])
    console.print()

    console.print("[bold]Interactive Features:[/bold]", style=COLORS["primary"])
    console.print("  Enter           Submit your message", style=COLORS["dim"])
    console.print(
        "  Alt+Enter       Insert newline for multi-line (Option+Enter or ESC then Enter)",
        style=COLORS["dim"],
    )
    console.print("  Ctrl+J          Insert newline (alternative)", style=COLORS["dim"])
    console.print("  Ctrl+T          Toggle auto-approve mode", style=COLORS["dim"])
    console.print("  Arrow keys      Navigate input", style=COLORS["dim"])
    console.print(
        "  @filename       Type @ to auto-complete files and inject content",
        style=COLORS["dim"],
    )
    console.print(
        "  /command        Type / to see available commands (auto-completes)",
        style=COLORS["dim"],
    )
    console.print()

    console.print("[bold]Interactive Commands:[/bold]", style=COLORS["primary"])
    console.print("  /help           Show available commands and features", style=COLORS["dim"])
    console.print("  /clear          Clear screen and reset conversation", style=COLORS["dim"])
    console.print("  /tokens         Show token usage for current session", style=COLORS["dim"])
    console.print("  /context        Show detailed context window usage", style=COLORS["dim"])
    console.print("  /compact        Summarize conversation to free context", style=COLORS["dim"])
    console.print(
        "  /verbose        Toggle verbose mode (show internal agent context)", style=COLORS["dim"]
    )
    console.print(
        "  /init           Explore codebase and create Nova.MD file",
        style=COLORS["dim"],
    )
    console.print("  /sessions       List and manage saved sessions", style=COLORS["dim"])
    console.print("  /save           Manually save current session", style=COLORS["dim"])
    console.print("  /quit, /exit    Exit the session", style=COLORS["dim"])
    console.print(
        "  quit, exit, q   Exit the session (just type and press Enter)",
        style=COLORS["dim"],
    )
    console.print()
