"""Configuration, constants, and model creation for the CLI.

This module provides centralized configuration management for the CLI, including:
- Color scheme and UI styling constants
- Environment variable parsing and settings management
- Project root detection and working directory management
- Rich console initialization with proper encoding

Key Components:
- create_model(): Factory function for creating chat models from configuration
- settings(): Global settings object with environment variable access
- COLORS: Color scheme constants for UI styling
- ASCII art banner for CLI startup

Supported Model Providers:
- OpenAI (default): Requires OPENAI_API_KEY
- Anthropic: Requires ANTHROPIC_API_KEY
- Ollama: Local models, requires OLLAMA_BASE_URL
- Google: Requires GOOGLE_API_KEY

Environment Configuration:
- API keys loaded from environment variables or .env files
- Configuration files in ~/.nova/ and .nova/
- Project-specific settings override global settings
"""

import os
import random
import re
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import dotenv
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from novacode_cli.brand import (
    TAGLINE,
    WORDMARK_WIDTH,
    art_for,
    compact_mark,
    get_accent_hex,
    wordmark,
)

dotenv.load_dotenv()

# Home directory for NOVA configuration
HOME_DIR = Path.home() / ".nova"

# Color scheme - Red & White Theme
COLORS = {
    "primary": "#ef4444",  # Bright red (primary actions, headings)
    "secondary": "#dc2626",  # Deep red (highlights, important text)
    "accent": "#fca5a5",  # Light red (accents, borders)
    "dim": "#9ca3af",  # Gray (secondary text)
    "user": "#ffffff",  # White (user messages)
    "agent": "#ef4444",  # Bright red (agent messages)
    "thinking": "#f87171",  # Medium red (thinking/processing)
    "tool": "#dc2626",  # Deep red (tool calls)
    "success": "#10b981",  # Green (success states)
    "warning": "#f59e0b",  # Orange (warnings)
    "error": "#dc2626",  # Deep red (errors)
    "subagent": "#30c3f0",  # Medium red (sub-agent messages)
}

# Reserved agent ID for the main agent — not a user-created named subagent.
MAIN_AGENT_ID = "nova-agent"

# Agent color registry - stores colors for custom agents
_agent_colors: dict[str, str] = {}


def extract_agent_description(agent_md: Path) -> str:
    """Extract description from agent.md YAML frontmatter or first content line.

    Args:
        agent_md: Path to the agent.md file.

    Returns:
        Extracted description string.
    """
    try:
        content = agent_md.read_text(encoding="utf-8")

        # YAML front-matter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                front_matter = parts[1]
                for line in front_matter.splitlines():
                    line = line.strip()
                    if line.startswith("description:"):
                        return line.split(":", 1)[1].strip()[:80]

        # Fallback: first non-empty, non-heading line
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return (line[:80] + "...") if len(line) > 80 else line

    except Exception:
        pass

    return "[unable to read]"


def get_agent_color(agent_name: str) -> str:
    """Get the color for an agent by name.

    Args:
        agent_name: The name of the agent.

    Returns:
        The color string (hex or name), or default subagent color if not set.
    """
    return _agent_colors.get(agent_name, COLORS["subagent"])


def set_agent_color(agent_name: str, color: str) -> None:
    """Set the color for an agent.

    Args:
        agent_name: The name of the agent.
        color: The color string (hex code like '#ef4444' or color name).
    """
    _agent_colors[agent_name] = color


def clear_agent_colors() -> None:
    """Clear all registered agent colors."""
    _agent_colors.clear()


def parse_agent_color(agent_md_path: Path) -> str | None:
    """Parse color from agent.md YAML frontmatter.

    Looks for a color field in YAML frontmatter at the start of the file:
    ```
    ---
    color: #ef4444
    ---
    ```

    Args:
        agent_md_path: Path to the agent.md file.

    Returns:
        Color string if found, None otherwise.
    """
    try:
        content = agent_md_path.read_text(encoding="utf-8")

        # Match YAML frontmatter between --- delimiters
        frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n"
        match = re.match(frontmatter_pattern, content, re.DOTALL)

        if not match:
            return None

        frontmatter = match.group(1)

        # Parse color from frontmatter
        for line in frontmatter.split("\n"):
            kv_match = re.match(r"^color:\s*(.+)$", line.strip())
            if kv_match:
                return kv_match.group(1).strip().strip('"').strip("'")

        return None
    except Exception:
        return None


def get_responsive_ascii(
    console: Console | None = None, width: int | None = None
) -> str:
    """Responsive startup ASCII art, sized to the terminal width.

    Thin adapter over :func:`novacode_cli.brand.art_for`, which owns the art.
    This used to inline three near-identical copies plus a fourth legacy
    constant, whose rows ranged from 29 to 77 columns wide.

    Args:
        console: Rich console to read the terminal width from, used only when
            ``width`` is not given.
        width: Explicit terminal width in columns. Prefer this from a Textual
            app (``self.size.width``), since the global Rich console width does
            not track the live TUI size on resize.

    Returns:
        ASCII art sized for the terminal.
    """
    terminal_width = width
    if terminal_width is None:
        try:
            terminal_width = console.width if console is not None else 80
        except Exception:
            terminal_width = 80
    return art_for(terminal_width, settings.version)


# Legacy static ASCII art. Kept as the name earlier code imported; the art
# itself now comes from brand.py so there is one wordmark, not five.
NOVA_CODE_ASCII = wordmark()

# Interactive commands
COMMANDS = {
    "clear": "Clear screen and reset conversation",
    "help": "Show help information",
    "tokens": "Show token usage for current session",
    "context": "Show detailed context window usage with visual breakdown",
    "compact": "Summarize conversation to free up context (e.g., /compact Focus on X)",
    "init": "Explore codebase and create NOVA.MD file",
    "mcp": "Manage MCP servers (install presets, add custom, list, remove)",
    "model": "Manage LLM providers (view, switch between OpenAI, Anthropic, Ollama, Google)",
    "hooks": "Manage hooks - list, add, remove, test, view logs (e.g., /hooks list)",
    "skills": "Manage skills - create or list (e.g., /skills, /skills create, /skills list)",
    "agents": "Manage custom agents - view, create, or delete (e.g., /agents)",
    "sessions": "List and manage saved sessions",
    "save": "Manually save current session (auto-saved on exit)",
    "servers": "List and manage running dev servers",
    "tests": "Run project tests (e.g., /tests or /tests pytest -v)",
    "trace": "Manage LangSmith tracing (status, enable, disable, projects)",
    "kill": "Kill a running process by PID or name (e.g., /kill 1234)",
    "images": "Manage images in conversation (list, remove <id>, clear)",
    "plan": "Toggle plan mode (e.g., /plan, /plan on, /plan off)",
    "verbose": "Toggle verbose mode - show/hide internal agent context",
    "steer": "Add persistent steering instructions (e.g., /steer focus on database layer)",
    "remote": "Manage remote bridges to Discord or Telegram (start/stop/status/test)",
    "cron": 'Schedule recurring agent tasks (e.g., /cron add "0 9 * * *" "review project")',
    "webhook": "Manage the webhook ingress server (start/stop/register/status)",
    "prompt": "Manage evolving system-prompt templates (status/rollback/accept/reject)",
    "voice": "Local voice I/O — status, on/off, mode ptt|listen, test (ctrl+g talk, ctrl+l listen)",
    "vision": "Analyze images with vision model (e.g., /vision @image.png, /vision @img1.png @img2.png)",
    "files": "Show file operation summary for the session",
    "critique": "Run critique agent on recent changes (e.g., /critique or /critique src/)",
    "ralph": "Run autonomous looping mode (e.g., /ralph <task>, /ralph <task> --iterations 5)",
    "browser-use": "Run browser automation with AI (e.g., /browser-use <task>, /browser-use <task> --model llama3.2)",
    "council": "Open the Council web UI (5 agents answer independently, then vote — majority wins)",
    "create": "Open the Skills & Agents web UI (browse, preview, edit, create skills and agents)",
    "dream": "Run memory consolidation to organize and clean up memory files",
    "research": "Run agent swarm research (e.g., /research <query>, /research academic <query>, /research market <query>)",
    "reindex": "Rebuild the semantic code search index (after significant code changes)",
    "trello": "Start a task board in the browser (add tasks, agent processes them one at a time)",
    "exit": "Exit the CLI",
}


# Maximum argument length for display
MAX_ARG_LENGTH = 150

# Tool icons for display in tool calls
TOOL_ICONS = {
    "read_file": "📄",
    "write_file": "✍️",
    "edit_file": "📝",
    "shell": "💻",
    "ls": "📁",
    "glob": "🔍",
    "grep": "🔎",
    "web_search": "🌐",
    "fetch_url": "📡",
    "fetch_url": "🌍",
    "task": "🤖",
    "write_todos": "📋",
    "mcp": "🔌",
    "run_tests": "🧪",
    "start_dev_server": "🚀",
    "stop_dev_server": "🛑",
    "list_servers": "📋",
    "code_search": "🔍",
    "find_related_code": "🔄",
    "default": "🔧",
}

# Agent configuration
config = {"recursion_limit": 1000}


def format_version_banner(version: str) -> str:
    """Return the styled version banner for ``nova --version``.

    Uses the shared wordmark from :mod:`novacode_cli.brand` — this used to
    inline a fourth, differently-sized copy of the logo (missing the braille
    half entirely and stamped with its own version row).
    """
    caption = f"NOVA · {TAGLINE} · v{version}"
    return f"\n{wordmark()}\n{caption.center(WORDMARK_WIDTH)}\n"


# Rich console instance
# Force UTF-8 encoding on Windows to support Unicode characters in ASCII art
if sys.platform == "win32":
    import io

    console = Console(
        highlight=False, file=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    )
else:
    console = Console(highlight=False)


# Fun rotating boot messages for animated startup
_BOOT_MESSAGES = [
    "Nova is launching…",
    "mcp: routing through the stars…",
    "mcp: establishing tool links…",
    "memory: warming the neural core…",
    "sandbox: spinning up isolation chamber…",
    "session: weaving context threads…",
    "skills: loading combat protocols…",
    "system: calibrating LLM interface…",
    "model: initializing cognition engine…",
    "tools: loading the arsenal…",
    "workspace: scanning the terrain…",
    "plugins: discovering extensions…",
    "engine: starting up the stacks…",
    "firewall: raising digital shields…",
    "backbone: synchronizing data buses…",
]

_BOOT_MESSAGE_INDEX: dict[str, list[str]] = {}
"""Tracks which messages have been used per subsystem for rotation."""


def _pick_boot_message(subsystem: str) -> str:
    """Pick a fun rotating message for a given subsystem.

    Cycles through available messages for that subsystem (matched by prefix),
    preferring unused ones first to keep startup varied.

    Args:
        subsystem: The message prefix/subsystem name (e.g. ``"mcp"``, ``"memory"``).

    Returns:
        A message string that hasn't been used this session (or a random one).
    """
    candidates = [m for m in _BOOT_MESSAGES if m.startswith(subsystem)]
    if not candidates:
        return f"{subsystem}: initializing…"

    if subsystem not in _BOOT_MESSAGE_INDEX:
        _BOOT_MESSAGE_INDEX[subsystem] = []

    used = _BOOT_MESSAGE_INDEX[subsystem]
    available = [c for c in candidates if c not in used]

    if available:
        choice = random.choice(available)
    else:
        # All used — pick a random one and wrap around
        choice = random.choice(candidates)

    used.append(choice)
    return choice


class BootRail:
    """An indeterminate progress rail that animates on elapsed time alone.

    A boot genuinely cannot report a fraction: only ``core_agent`` emits an
    unconditional ``boot_status`` (at the *start* of MCP discovery, which then
    blocks for seconds emitting nothing), while voice, session, sandbox, memory
    and shell are all conditional. So a ``done/total`` ratio has no honest
    inputs — the denominator is usually 1, which pinned the previous rail at a
    constant 0% for the entire boot.

    This renderable therefore reports *activity*, not *completion*: a span
    sweeps the track, position derived purely from wall-clock elapsed time. It
    animates during any silent phase (e.g. MCP discovery) without a single
    ``boot_status`` call, and it never claims a percentage it cannot know.

    Colour is applied by the caller via style strings, not here, so the bar
    still reads correctly on consoles without truecolor.
    """

    #: Number of character cells in the sweeping highlight.
    COMET = 6
    #: Cells of travel per second — fast enough to read as "working".
    SPEED = 14.0
    #: Track length in cells.
    WIDTH = 28

    def __init__(
        self,
        width: int = WIDTH,
        accent: str = "cyan",
        start: float | None = None,
    ) -> None:
        """Store the track width, highlight colour and the animation epoch."""
        self.width = width
        self.accent = accent
        self._start = time.monotonic() if start is None else start

    def frame(self, elapsed: float) -> Text:
        """The rail at *elapsed* seconds, as styled text.

        Pure in ``elapsed``, so the geometry is testable without a display and
        ``__rich_console__`` just feeds it the wall clock.
        """
        span = self.width + self.COMET
        head = int(max(elapsed, 0.0) * self.SPEED) % span
        track = Text()
        for i in range(self.width):
            if head - self.COMET < i <= head:
                track.append("━", style=self.accent)
            else:
                track.append("─", style="grey30")
        return track

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Yield the current frame.

        Re-rendered on every ``Live`` refresh tick (~12/s), which is what makes
        the rail keep moving while the boot is silent.
        """
        yield from console.render(self.frame(time.monotonic() - self._start), options)


class BootAnimation:
    """Animated startup sequence with live spinner and accumulating status lines.

    Use as a context manager around startup code. Any call to ``boot_status()``
    inside the context is automatically rendered as an animated line with a
    Rich spinner on the current message and checkmarks on completed ones.

    Usage::

        with BootAnimation.start():
            boot_status("Nova is launching…")
            time.sleep(2)
            boot_status("mcp: all servers online", "ok")
            boot_status("memory: initializing…")
            time.sleep(1)
            boot_status("memory: store ready", "ok")
        # animation exits — clean transition to splash screen
    """

    _instance: "BootAnimation | None" = None
    _live: Live | None = None
    _messages: list[tuple[str, str]] = []
    _start_time: float = 0.0

    @classmethod
    @contextmanager
    def start(cls) -> None:
        """Open the live display (sync version). Yields once, then tears down.

        There is no ``total_steps`` argument: the progress rail derives its
        denominator from the phases actually reported, so it cannot be told a
        number the boot will never reach.
        """
        cls._messages = []
        cls._start_time = time.monotonic()

        layout = Table.grid(padding=(0, 1))
        layout.add_column(no_wrap=True)

        with Live(
            layout,
            console=console,
            refresh_per_second=12,
            transient=True,
        ) as live:
            cls._live = live
            cls._instance = cls()
            try:
                yield
            finally:
                cls._instance = None
                cls._live = None

    @classmethod
    @asynccontextmanager
    async def async_start(cls) -> None:
        """Open the live display (async version). Yields once, then tears down.

        Safe to use with ``async with`` in async functions (e.g. ``main()``).
        Rich's ``Live.__enter__`` starts a background rendering thread which
        is compatible with async event loops.

        Like :meth:`start`, no ``total_steps``: the rail is derived from the
        phases actually reported.
        """
        cls._messages = []
        cls._start_time = time.monotonic()

        layout = Table.grid(padding=(0, 1))
        layout.add_column(no_wrap=True)

        live = Live(layout, console=console, refresh_per_second=12, transient=True)
        live.__enter__()
        cls._live = live
        cls._instance = cls()
        try:
            yield
        finally:
            cls._instance = None
            cls._live = None
            live.__exit__(None, None, None)

    @classmethod
    def status(cls, message: str, level: str = "info") -> None:
        """Add or update a status line.

        ``"info"`` level messages are considered "in progress" — the most recent
        one gets a spinner. ``"ok"`` and ``"warn"`` are "completed" and rendered
        with checkmark or warning glyph.

        Args:
            message: Short ``"subsystem: detail"`` message.
            level: ``"info"`` (in-progress with spinner), ``"ok"`` (green ✓),
                or ``"warn"`` (yellow ⚠).
        """
        # Replace last pending "info" with the new one (live-update the spinner
        # text), otherwise append as a new line.
        if level == "info":
            if cls._messages and cls._messages[-1][1] == "info":
                cls._messages[-1] = (message, level)
            else:
                cls._messages.append((message, level))
        else:
            cls._messages.append((message, level))
        cls._refresh()

    @staticmethod
    def _phase(message: str) -> tuple[str, str]:
        """Split a ``"subsystem: detail"`` status into ``(label, detail)``.

        The staged boot list reads as two aligned columns, so the label and the
        detail are rendered separately. A message with no colon is all label.
        """
        label, sep, detail = message.partition(":")
        if not sep:
            return message.strip(), ""
        return label.strip(), detail.strip()

    @classmethod
    def _refresh(cls) -> None:
        """Rebuild and update the live staged boot display.

        Three changes from the old splash, each fixing something real:

        * **The rail no longer lies.** It is indeterminate: a span sweeps a
          track on elapsed time, because the boot reports no usable fraction
          (see :class:`BootRail`). An earlier attempt derived ``done/total``
          from the phases observed, which pinned the bar at 0% for an entire
          real boot — the denominator is 1 whenever nothing has completed yet.
        * **One art source.** The header is ``brand.compact_mark()``; the old
          splash inlined a 12-line braille blob spliced to a second ANSI
          wordmark, with rows ranging from 29 to 77 columns wide.
        * **Theme accent.** Colours come from ``brand.get_accent_hex()``, so the
          boot screen is tinted for the theme the TUI is about to open with --
          previously it was hardcoded red and the app opened blue.
        """
        if not cls._live:
            return

        elapsed = time.monotonic() - cls._start_time
        accent = get_accent_hex()
        layout = Table.grid(padding=(0, 1))
        layout.add_column(no_wrap=True)

        # ── Header: the compact mark (see brand.py) ──
        header = Text()
        header.append("  ")
        header.append(compact_mark(), style=f"bold {accent}")
        layout.add_row(header)

        caption = Text("  ", style="grey30")
        caption.append(f"{TAGLINE}  ·  v{settings.version}", style="grey30")
        layout.add_row(caption)
        layout.add_row(Text(""))

        # ── Staged checklist: one aligned row per phase ──
        # The last entry is split off only when it is still in progress; a
        # finished boot therefore shows every phase with its checkmark and no
        # lingering spinner (a spinner on a completed phase reads as "still
        # working" right up until the screen vanishes).
        label_w = max((len(cls._phase(m)[0]) for m, _ in cls._messages), default=0)
        glyphs = {"ok": ("✓", "green"), "warn": ("⚠", "yellow")}
        active = cls._messages[-1] if cls._messages and cls._messages[-1][1] == "info" else None
        done_msgs = cls._messages[:-1] if active else cls._messages
        for msg, lvl in done_msgs:
            label, detail = cls._phase(msg)
            glyph, style = glyphs.get(lvl, ("·", "grey42"))
            row = Text()
            row.append(f"  {glyph} ", style=style)
            row.append(label.ljust(label_w), style=f"bold {accent}")
            if detail:
                row.append(f"  {detail}", style="default")
            layout.add_row(row)

        # ── Active phase, with a live spinner ──
        if active:
            label, detail = cls._phase(active[0])
            text = f"{label.ljust(label_w)}  {detail}".rstrip()
            layout.add_row(Spinner("dots2", text=f"  {text}", style=f"bold {accent}"))

        # ── Honest progress rail ──
        # Deliberately NOT a percentage. Only ``core_agent`` emits an
        # unconditional status (at the start of MCP discovery); every other
        # subsystem is conditional, so ``done/total`` was nearly always 0/1 and
        # the old rail sat pinned at 0% for the whole boot. A sweeping rail
        # reports activity, which is the one thing that is always true.
        rail_row = Text("  ")
        rail_row.append_text(cls._rail(accent))
        rail_row.append(f"   {elapsed:.1f}s", style="grey30")
        layout.add_row(rail_row)
        cls._live.update(layout)

    @classmethod
    def _rail(cls, accent: str) -> Text:
        """The rail for the current instant; shared by ``_refresh`` and tests."""
        return BootRail(accent=accent, start=cls._start_time).frame(
            time.monotonic() - cls._start_time
        )


def boot_status(message: str, level: str = "info") -> None:
    """Print a uniform, minimal startup status line.

    When inside a ``BootAnimation`` context, the message is rendered with a
    live spinner animation instead of a static line. Outside the context,
    falls back to a simple dimmed status line.

    Keeps Nova's launch output tidy: one muted glyph per line, a consistent
    palette, and short ``subsystem: detail`` messages instead of a mix of
    colors and emoji.

    Args:
        message: Short status, ideally ``"subsystem: detail"`` (e.g.
            ``"sandbox: docker 3b7334a ready"``).
        level: ``"info"`` (dim), ``"ok"`` (green), or ``"warn"`` (yellow).
    """
    # Delegate to BootAnimation when active — renders with live spinner
    if BootAnimation._instance is not None:
        BootAnimation.status(message, level)
        return

    # Outside animation context: static line
    from rich.text import Text

    glyph, style = {
        "info": ("·", "grey50"),
        "ok": ("✓", "green"),
        "warn": ("⚠", "yellow"),
    }.get(level, ("·", "grey50"))
    console.print(Text(f"  {glyph} {message}", style=style))


# Cache for project skills directories with TTL
_project_skills_cache: dict[str, tuple[float, list[Path]]] = {}
_PROJECT_SKILLS_CACHE_TTL = 30.0  # seconds


def find_project_skills(project_root: Path) -> list[Path]:
    """Find project-specific skills directories.

    Checks for skills in both .claude/ and .nova/ directories.
    Uses a cache with TTL to avoid repeated filesystem scans.

    Args:
        project_root: Path to the project root directory.

    Returns:
        List of skills directory paths that exist.
    """
    import time

    # Check cache first
    cache_key = str(project_root)
    now = time.time()
    if cache_key in _project_skills_cache:
        cached_time, cached_value = _project_skills_cache[cache_key]
        if now - cached_time < _PROJECT_SKILLS_CACHE_TTL:
            return cached_value

    skills_dirs = []

    # Check .claude/skills/
    claude_skills = project_root / ".claude" / "skills"
    if claude_skills.exists() and claude_skills.is_dir():
        skills_dirs.append(claude_skills)

    # Check .nova/skills/
    deepagents_skills = project_root / ".nova" / "skills"
    if deepagents_skills.exists() and deepagents_skills.is_dir():
        skills_dirs.append(deepagents_skills)

    # Cache the result
    _project_skills_cache[cache_key] = (now, skills_dirs)

    return skills_dirs


def _find_project_root(start_path: Path | None = None) -> Path | None:
    """Find the project root by looking for .git directory.

    Walks up the directory tree from start_path (or cwd) looking for a .git
    directory, which indicates the project root.

    Args:
        start_path: Directory to start searching from. Defaults to current working directory.

    Returns:
        Path to the project root if found, None otherwise.
    """
    current = Path(start_path or Path.cwd()).resolve()

    # Walk up the directory tree
    for parent in [current, *list(current.parents)]:
        git_dir = parent / ".git"
        if git_dir.exists():
            return parent

    return None


def _find_project_agent_md(project_root: Path) -> list[Path]:
    """Find ALL project-specific CLAUDE.md and Nova.md files.

    Returns every memory file that exists, ordered from most general to most
    specific (matching Claude Code's hierarchical loading behavior). All found
    files are combined so later entries take higher precedence.

    Load order (general → specific):
    1. project_root/NOVA.md       (NOVA root — created by /init)
    2. project_root/.nova/NOVA.md (NOVA directory)
    3. project_root/CLAUDE.md     (Claude Code root)
    4. project_root/.claude/CLAUDE.md (Claude Code directory — highest precedence)

    Args:
        project_root: Path to the project root directory.

    Returns:
        List of existing paths (may be empty if no memory files found).
    """
    candidates = [
        project_root / "NOVA.md",
        project_root / ".nova" / "NOVA.md",
        project_root / "CLAUDE.md",
        project_root / ".claude" / "CLAUDE.md",
    ]
    return [p for p in candidates if p.exists()]


def get_default_coding_instructions() -> str:
    """Get the default coding agent instructions.

    These are the immutable base instructions that cannot be modified by the agent.
    Long-term memory (agent.md) is handled separately by the middleware.
    """
    from novacode_cli.prompts import render_template

    from novacode_cli.agents.default_subagents.async_subagents import (
        async_agents_available,
    )

    return render_template(
        "System_Prompt_Nova.jinja",
        has_tavily=settings.has_tavily,
        # The async subagents exist only while their LangGraph container runs.
        # Describing tools that are not bound wastes context and invites the
        # model to call something that isn't there.
        has_async_agents=async_agents_available(),
    )


@dataclass
class Settings:
    """Global settings and environment detection for deepagents-cli.

    This class is initialized once at startup and provides access to:
    - Available models and API keys
    - Current project information
    - Tool availability (e.g., Tavily)
    - File system paths

    Attributes:
        project_root: Current project root directory (if in a git project)

        openai_api_key: OpenAI API key if available
        anthropic_api_key: Anthropic API key if available
        tavily_api_key: Tavily API key if available
    """

    # API keys
    openai_api_key: str | None
    anthropic_api_key: str | None
    google_api_key: str | None
    openrouter_api_key: str | None
    opencode_api_key: str | None
    nvidia_api_key: str | None
    tavily_api_key: str | None
    langsmith_api_key: str | None

    # Ollama configuration
    ollama_host: str | None

    # Project information
    project_root: Path | None

    # LangSmith configuration
    langsmith_project: str = "Nova-Code"
    langsmith_workspace_id: str | None = None
    langsmith_tracing_enabled: bool = False

    version: str = "1.0.0"

    @classmethod
    def from_environment(cls, *, start_path: Path | None = None) -> "Settings":
        """Create settings by detecting the current environment.

        Priority order for API keys:
        1. OS keychain (via SecretManager)
        2. Environment variables
        3. None (not configured)

        Args:
            start_path: Directory to start project detection from (defaults to cwd)

        Returns:
            Settings instance with detected configuration
        """
        # Import SecretManager here to avoid circular imports
        # (SecretManager imports from config, config imports SecretManager)
        try:
            from novacode_cli.onboarding import SecretManager

            secret_manager = SecretManager()
        except ImportError:
            # If onboarding module not available yet, skip keyring
            secret_manager = None

        # Detect API keys - check keyring first, then environment variables
        if secret_manager:
            openai_key = secret_manager.get_secret("openai_api_key") or os.environ.get(
                "OPENAI_API_KEY"
            )
            anthropic_key = secret_manager.get_secret(
                "anthropic_api_key"
            ) or os.environ.get("ANTHROPIC_API_KEY")
            google_key = secret_manager.get_secret("google_api_key") or os.environ.get(
                "GOOGLE_API_KEY"
            )
            openrouter_key = secret_manager.get_secret(
                "openrouter_api_key"
            ) or os.environ.get("OPENROUTER_API_KEY")
            opencode_key = secret_manager.get_secret(
                "opencode_api_key"
            ) or os.environ.get("OPENCODE_API_KEY")
            nvidia_key = secret_manager.get_secret(
                "nvidia_api_key"
            ) or os.environ.get("NVIDIA_API_KEY")
            tavily_key = secret_manager.get_secret("tavily_api_key") or os.environ.get(
                "TAVILY_API_KEY"
            )
        else:
            openai_key = os.environ.get("OPENAI_API_KEY")
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
            google_key = os.environ.get("GOOGLE_API_KEY")
            openrouter_key = os.environ.get("OPENROUTER_API_KEY")
            opencode_key = os.environ.get("OPENCODE_API_KEY")
            nvidia_key = os.environ.get("NVIDIA_API_KEY")
            tavily_key = os.environ.get("TAVILY_API_KEY")

        langsmith_key = os.environ.get("LANGSMITH_API_KEY")

        # Detect Ollama host configuration
        ollama_host = os.environ.get("OLLAMA_HOST")

        # Detect project
        project_root = _find_project_root(start_path)

        # LangSmith configuration
        langsmith_enabled = os.environ.get("LANGSMITH_TRACING", "").lower() == "true"
        langsmith_project = os.environ.get("LANGSMITH_PROJECT", "Nova-Code")
        langsmith_workspace = os.environ.get("LANGSMITH_WORKSPACE_ID")

        return cls(
            openai_api_key=openai_key,
            anthropic_api_key=anthropic_key,
            google_api_key=google_key,
            openrouter_api_key=openrouter_key,
            opencode_api_key=opencode_key,
            nvidia_api_key=nvidia_key,
            tavily_api_key=tavily_key,
            langsmith_api_key=langsmith_key,
            ollama_host=ollama_host,
            project_root=project_root,
            langsmith_project=langsmith_project,
            langsmith_workspace_id=langsmith_workspace,
            langsmith_tracing_enabled=langsmith_enabled,
        )

    @property
    def has_openai(self) -> bool:
        """Check if OpenAI API key is configured."""
        return self.openai_api_key is not None

    @property
    def has_anthropic(self) -> bool:
        """Check if Anthropic API key is configured."""
        return self.anthropic_api_key is not None

    @property
    def has_google(self) -> bool:
        """Check if Google API key is configured."""
        return self.google_api_key is not None

    @property
    def has_openrouter(self) -> bool:
        """Check if OpenRouter API key is configured."""
        return self.openrouter_api_key is not None

    @property
    def has_opencode(self) -> bool:
        """Check if OpenCode Go API key is configured."""
        return self.opencode_api_key is not None

    @property
    def has_nvidia(self) -> bool:
        """Check if an NVIDIA NIM API key is configured."""
        return self.nvidia_api_key is not None

    @property
    def has_tavily(self) -> bool:
        """Check if Tavily API key is configured."""
        return self.tavily_api_key is not None

    @property
    def has_langsmith(self) -> bool:
        """Check if LangSmith is configured and enabled."""
        return self.langsmith_api_key is not None and self.langsmith_tracing_enabled

    @property
    def has_project(self) -> bool:
        """Check if currently in a git project."""
        return self.project_root is not None

    def get_workspace_root(self) -> Path:
        """The project directory — THE root every part of Nova works in.

        The git repository root whenever Nova is launched anywhere inside one,
        otherwise the (resolved) launch directory.

        This used to return the launch directory when it was a subfolder of the
        repo, which split Nova in two: NOVA.md and the system prompt described
        the whole repository while the file tools could only reach the
        subfolder, so ``write_file("/tests/test_x.py")`` landed in
        ``<subfolder>/tests/``. Launching from the repo root and from a
        subfolder now mean the same project, as they do in Claude Code.

        Always returns a resolved path, so callers can compare roots directly;
        comparing an unresolved cwd to a resolved root broke on ``subst``
        drives and junctions.
        """
        if self.project_root:
            return Path(self.project_root)
        return Path.cwd().resolve()

    @property
    def has_graph(self) -> bool:
        """Check if a project graph is available (built by /init).

        Returns:
            True if `.nova/project-graph.json` exists under the project root.
        """
        if not self.project_root:
            return False
        return (self.project_root / ".nova" / "project-graph.json").exists()

    def get_onboarding_status(self) -> bool:
        """Check if onboarding has been completed.

        Returns:
            True if onboarding completed, False otherwise
        """
        config_file = HOME_DIR / "config.json"
        onboarded_marker = HOME_DIR / ".onboarded"

        # Check if either marker exists
        if onboarded_marker.exists():
            return True

        # Check config.json for onboarding_completed flag
        if config_file.exists():
            try:
                import json

                config = json.loads(config_file.read_text(encoding="utf-8"))
                return config.get("onboarding_completed", False)
            except Exception:  # noqa: BLE001
                return False

        return False

    def mark_onboarding_complete(self) -> None:
        """Mark onboarding as completed.

        Creates completion markers in config.json and .onboarded file.
        """
        import json

        config_file = HOME_DIR / "config.json"
        onboarded_marker = HOME_DIR / ".onboarded"

        # Update config.json
        config = {}
        if config_file.exists():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001, S110
                pass

        config["onboarding_completed"] = True
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

        # Create marker file
        onboarded_marker.touch()

    @property
    def user_deepagents_dir(self) -> Path:
        """Get the base user-level .nova directory.

        Returns:
            Path to ~/.nova
        """
        return HOME_DIR

    @property
    def nova_dir(self) -> Path:
        """Base user-level .nova directory (~/.nova).

        Alias of :attr:`user_deepagents_dir`; used for paths like checkpoints
        and other per-user state under ~/.nova.
        """
        return HOME_DIR

    def get_agents_root_dir(self) -> Path:
        """Get the global agents root directory.

        Returns:
            Path to ~/.nova/agents/
        """
        return self.user_deepagents_dir / "agents"

    def get_project_agents_dir(self) -> Path | None:
        """Get project-level agents directory path.

        Returns:
            Path to {project_root}/.nova/agents/, or None if not in a project
        """
        if not self.project_root:
            return None
        return self.project_root / ".nova" / "agents"

    def ensure_project_agents_dir(self) -> Path | None:
        """Ensure project-level agents directory exists and return its path.

        Returns:
            Path to {project_root}/.nova/agents/, or None if not in a project
        """
        if not self.project_root:
            return None
        agents_dir = self.get_project_agents_dir()
        if agents_dir:
            agents_dir.mkdir(parents=True, exist_ok=True)
        return agents_dir

    def get_all_agents(self) -> list[tuple[str, Path, str]]:
        """Get all available agents from both global and project scopes.

        Returns:
            List of (agent_name, agent_dir_path, scope) tuples.
            scope is either "global" or "project"
        """
        agents: list[tuple[str, Path, str]] = []

        # Global agents (~/.nova/agents/)
        global_agents_dir = self.get_agents_root_dir()
        if global_agents_dir.exists():
            for agent_dir in global_agents_dir.iterdir():
                if agent_dir.is_dir() and (agent_dir / "agent.md").exists():
                    if agent_dir.name == MAIN_AGENT_ID:
                        continue  # reserved — not a user-created named agent
                    agents.append((agent_dir.name, agent_dir, "global"))

        # Project agents ({project_root}/.nova/agents/)
        project_agents_dir = self.get_project_agents_dir()
        if project_agents_dir and project_agents_dir.exists():
            for agent_dir in project_agents_dir.iterdir():
                if agent_dir.is_dir() and (agent_dir / "agent.md").exists():
                    if agent_dir.name == MAIN_AGENT_ID:
                        continue  # reserved — not a user-created named agent
                    agents.append((agent_dir.name, agent_dir, "project"))

        return agents

    def find_agent(self, agent_name: str) -> tuple[Path, str] | None:
        """Find an agent by name, checking project scope first then global.

        Project-specific agents take precedence over global agents with the same name.

        Args:
            agent_name: Name of the agent to find

        Returns:
            Tuple of (agent_dir_path, scope) if found, None otherwise
        """
        # Check project agents first (higher priority)
        project_agents_dir = self.get_project_agents_dir()
        if project_agents_dir:
            project_agent_dir = project_agents_dir / agent_name
            if project_agent_dir.exists() and (project_agent_dir / "agent.md").exists():
                return (project_agent_dir, "project")

        # Check global agents
        global_agent_dir = self.get_agents_root_dir() / agent_name
        if global_agent_dir.exists() and (global_agent_dir / "agent.md").exists():
            return (global_agent_dir, "global")

        return None

    def get_global_skills_dir(self) -> Path:
        """Get the global skills directory (shared across all agents).

        Returns:
            Path to ~/.nova/skills/
        """
        return self.user_deepagents_dir / "skills"

    def get_user_agent_md_path(self, agent_name: str) -> Path:
        """Get user-level agent.md path for a specific agent.

        Returns path regardless of whether the file exists.

        Args:
            agent_name: Name of the agent

        Returns:
            Path to ~/.nova/agents/{agent_name}/agent.md
        """
        return self.get_agents_root_dir() / agent_name / "agent.md"

    def get_project_agent_md_path(self) -> Path | None:
        """Get project-level agent.md path (legacy single-file method).

        DEPRECATED: Use get_project_agent_md_paths() for full multi-file support.

        Returns path regardless of whether the file exists.

        Returns:
            Path to {project_root}/.nova/NOVA.md, or None if not in a project
        """
        if not self.project_root:
            return None
        return self.project_root / ".nova" / "NOVA.md"

    def get_project_agent_md_paths(self) -> list[Path]:
        """Get all project-level memory file paths (CLAUDE.md, NOVA.md).

        Returns every memory file that exists at the project root, ordered from
        most general to most specific — matching Claude Code's hierarchical
        loading behavior where all found files are combined.

        Returns:
            List of existing paths (empty if not in a project or no files found).
        """
        if not self.project_root:
            return []
        return _find_project_agent_md(self.project_root)

    @staticmethod
    def _is_valid_agent_name(agent_name: str) -> bool:
        """Validate prevent invalid filesystem paths and security issues."""
        if not agent_name or not agent_name.strip():
            return False
        # Allow only alphanumeric, hyphens, underscores, and whitespace
        return bool(re.match(r"^[a-zA-Z0-9_\-\s]+$", agent_name))

    def get_agent_dir(self, agent_name: str) -> Path:
        """Get the global agent directory path.

        Args:
            agent_name: Name of the agent

        Returns:
            Path to ~/.nova/agents/{agent_name}
        """
        if not self._is_valid_agent_name(agent_name):
            msg = (
                f"Invalid agent name: {agent_name!r}. "
                "Agent names can only contain letters, numbers, hyphens, underscores, and spaces."
            )
            raise ValueError(msg)
        return self.get_agents_root_dir() / agent_name

    def ensure_agent_dir(self, agent_name: str) -> Path:
        """Ensure the global agent directory exists and return its path.

        Args:
            agent_name: Name of the agent

        Returns:
            Path to ~/.nova/agents/{agent_name}
        """
        if not self._is_valid_agent_name(agent_name):
            msg = (
                f"Invalid agent name: {agent_name!r}. "
                "Agent names can only contain letters, numbers, hyphens, underscores, and spaces."
            )
            raise ValueError(msg)
        agent_dir = self.get_agent_dir(agent_name)
        agent_dir.mkdir(parents=True, exist_ok=True)
        return agent_dir

    def ensure_project_deepagents_dir(self) -> Path | None:
        """Ensure the project .nova directory exists and return its path.

        Returns:
            Path to project .nova directory, or None if not in a project
        """
        if not self.project_root:
            return None

        project_deepagents_dir = self.project_root / ".nova"
        project_deepagents_dir.mkdir(parents=True, exist_ok=True)
        return project_deepagents_dir

    def get_user_skills_dir(self, agent_name: str | None = None) -> Path:
        """Get user-level skills directory path (global, shared across agents).

        Args:
            agent_name: DEPRECATED - kept for backward compatibility, ignored.
                       Skills are now global at ~/.nova/skills/

        Returns:
            Path to ~/.nova/skills/ (global skills directory)
        """
        # Skills are now global, not per-agent
        return self.get_global_skills_dir()

    def ensure_user_skills_dir(self, agent_name: str | None = None) -> Path:
        """Ensure user-level skills directory exists and return its path.

        Args:
            agent_name: DEPRECATED - kept for backward compatibility, ignored.
                       Skills are now global at ~/.nova/skills/

        Returns:
            Path to ~/.nova/skills/ (global skills directory)
        """
        # Skills are now global, not per-agent
        skills_dir = self.get_global_skills_dir()
        skills_dir.mkdir(parents=True, exist_ok=True)
        return skills_dir

    @staticmethod
    def get_global_claude_skills_dir() -> Path:
        """Get the global Claude Code skills directory path.

        Returns:
            Path to ~/.claude/skills/ (global Claude Code skills directory)
        """
        return Path.home() / ".claude" / "skills"

    def get_project_skills_dir(self) -> Path | None:
        """Get project-level skills directory path (legacy .nova/skills/).

        Returns:
            Path to {project_root}/.nova/skills/, or None if not in a project
        """
        if not self.project_root:
            return None
        return self.project_root / ".nova" / "skills"

    def get_project_skills_dirs(self) -> list[Path]:
        """Get all project-level skills directories (both .claude/ and .nova/).

        Checks both:
        - {project_root}/.claude/skills/
        - {project_root}/.nova/skills/

        Returns:
            List of existing skills directory paths (may be empty if not in a project)
        """
        if not self.project_root:
            return []

        return find_project_skills(self.project_root)

    def ensure_project_skills_dir(self) -> Path | None:
        """Ensure project-level skills directory exists and return its path.

        Returns:
            Path to {project_root}/.nova/skills/, or None if not in a project
        """
        if not self.project_root:
            return None
        skills_dir = self.get_project_skills_dir()
        if skills_dir:
            skills_dir.mkdir(parents=True, exist_ok=True)
        return skills_dir


# Global settings instance (initialized once)
settings = Settings.from_environment()
