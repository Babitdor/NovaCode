"""Textual chat application (Phase 1).

A scrollable transcript + input + status line. The agent runs in a Textual
worker that iterates :func:`novacode_cli.agent_stream.run_agent_stream` and
renders each :mod:`novacode_cli.ui_events` event. HITL interrupts are shown as
modal screens.

Existing ``rich`` renderers are reused by capturing their output to a ``Text``
(``_capture``), so the visual style matches the legacy UI without duplicating
rendering code.

Animations
----------
All animated effects (entrance slide/fade/zoom, pulsing borders, shimmer,
thinking dots) are defined in :mod:`novacode_cli.tui.animations` and called
from ``on_mount`` handlers via Python's ``animate()`` API.
"""

from __future__ import annotations
from novacode_cli.core import subagent_tasks
from novacode_cli.prompts import render_template
from novacode_cli.ui import status_phrases
from novacode_cli.tui.output_buffer import MAX_PENDING_CALLS, OutputTail

import asyncio
import contextlib
import logging
import json
import re
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from textual.timer import Timer

    # Annotation-only: the clipboard image is passed straight to the tracker, so
    # the module is never imported at runtime on this path.
    from novacode_cli.image_utils import ImageData

    # Also annotation-only: the palette module is imported lazily at call sites
    # (see _active_palette), so this adds no module-scope import cost.
    from novacode_cli.tui.palette import FooterPalette
    from novacode_cli.tui.session_pane import SessionPane

from textual.binding import Binding
from novacode_cli.tui.harness import HarnessDock, UIHarness, UIHarnessRequest

logger = logging.getLogger(__name__)

from rich.markup import escape as _esc
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    ContentSwitcher,
    Input,
    OptionList,
    Static,
    Tab,
    Tabs,
)
from textual.widgets.option_list import Option
from novacode_cli.tui.session_tab import SessionTab

from novacode_cli.tui.animations import (
    animate_entrance,
)
from novacode_cli.tui import motion
from novacode_cli.tui.output_buffer import MAX_PENDING_CALLS, OutputTail

# Widgets and modal screens were extracted verbatim into widgets.py /
# screens.py. Re-exported here so `from novacode_cli.tui.app import X`
# keeps working for tests, main.py, and remote code.
# Markdown, cached per width: see CachedMarkdown for what the plain one cost.
from novacode_cli.tui.widgets import CachedMarkdown as Markdown
from novacode_cli.tui.widgets import OutputLog
from novacode_cli.tui.widgets import (
    DEFAULT_THEME,
    NOVA_MATRIX,
    NOVA_TOKYO_NIGHT,
    ChatMessage,
    MatrixRain,
    NovaStatusBar,
    PromptInput,
    QuestionDock,
    SessionHeader,
    SubagentsDock,
    TranscriptScroll,
    TuiInitRenderer,
)
from novacode_cli.tui.screens import (
    AgentCreateModal,
    AgentsScreen,
    ApprovalModal,
    BackgroundTasksScreen,
    ClaudePluginsScreen,
    ConfirmModal,
    ContextScreen,
    HookCreateModal,
    HooksScreen,
    InfoListScreen,
    McpCustomModal,
    McpInstallModal,
    McpScreen,
    ModelScreen,
    PickScreen,
    PlanApprovalModal,
    PluginsScreen,
    ProjectSessionPicker,
    QuestionModal,
    RalphScreen,
    RememberRuleModal,
    RemoteScreen,
    RouterScreen,
    ServersScreen,
    SessionsScreen,
    SettingsScreen,
    SkillCreateModal,
    SkillsScreen,
    SubagentPreviewScreen,
    ThemeScreen,
    WikiScreen,
)


# Transcript is pruned from the top once it exceeds this many widgets, down to
# _TRANSCRIPT_LOW_WATER — keeps Textual's layout/scroll/repaint fast in long
# sessions (the DOM would otherwise grow without bound).
#
# Sized from measured reflow cost, which is linear in widget count:
#     50 widgets -> 3.7 ms      250 -> 9.5 ms
#    150 widgets -> 5.6 ms      400 -> 13.8 ms
# The spinner ticks at 20 Hz (a 50 ms frame budget), so at 400 widgets a single
# layout pass ate ~28% of every frame — re-laying-out hundreds of widgets that
# are scrolled far out of view. 200 halves that to ~7 ms while still holding
# well over a screenful of scrollback (a full-height terminal shows ~40 rows).
_MAX_TRANSCRIPT_WIDGETS = 200
_TRANSCRIPT_LOW_WATER = 150
_MAX_TRANSCRIPT_CHARS = 2_000_000
_TRANSCRIPT_CHARS_LOW_WATER = 1_500_000

# How many subagent log lines to draw. The subagent card is a live progress view,
# not a scrollback: only the tail is visible, and rendering every entry on every
# event made subagent events O(n^2) (see _refresh_subagent_list).
_SUBAGENT_LIST_TAIL = 100

# Responsive breakpoints (terminal columns/rows). Below _NARROW_WIDTH the info
# bar sheds its widest columns and the status line drops its right-side counts;
# below the _MIN_* floor the layout can't fit and we surface a "too small" note.
_NARROW_WIDTH = 90
_COMPACT_WIDTH = 68
_MIN_WIDTH = 50
_MIN_HEIGHT = 12
_SHORT_HEIGHT = 20


def _responsive_breakpoints(width: int, height: int) -> tuple[bool, bool, bool, bool]:
    """Map a terminal size to (narrow, compact, short, tiny) layout classes."""
    return (
        width < _NARROW_WIDTH,
        width < _COMPACT_WIDTH,
        height < _SHORT_HEIGHT,
        width < _MIN_WIDTH or height < _MIN_HEIGHT,
    )


def _router_model_display(
    router_enabled: bool, current_model: str | None, routed_model: str | None
) -> tuple[str, str]:
    """Return the info-bar label and model value for the current router state."""
    if router_enabled:
        return "DYNAMIC (ROUTER MODE)", f"MODEL · {routed_model or current_model or '—'}"
    return "MODEL", current_model or "—"


# Tools whose result is a code change worth seeing in full: these keep their own
# Collapsible with a colored diff body so the user can review what the agent
# changed. Every other tool (reads, search, exec, MCP, …) condenses into the
# shared tool group. Keep in sync with the file-write tools that emit a FileOp
# record with a diff (see tracking/file_tracker.py).
_DETAILED_TOOL_NAMES = frozenset(
    {
        "write_file",
        "edit_file",
        "create_file",
        "multi_edit",
        "str_replace",
        "apply_patch",
    }
)


# Tool calls are grouped under a category heading in the condensed tool panel
# (e.g. "Explored — 3 reads"), so a long run of tools reads as a few labelled
# sections instead of one flat list. Order here drives the section order.
#
# Coverage: every tool the agent can call — the @tool functions in
# novacode_cli/tools/, deepagents' built-in filesystem/subagent/todo tools, and
# MCP tools (which arrive prefixed as "<server>_<tool>", matched by prefix
# below). A tool matching nothing falls into "Other".
_TOOL_CATEGORIES: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "Explored",
        "→",
        frozenset(
            {
                # Filesystem reads (deepagents built-ins + aliases)
                "read_file",
                "read",
                "view",
                "ls",
                "list_dir",
                "glob",
                "grep",
                "search",
                # Semantic / symbol search
                "code_search",
                "find_related_code",
                "find_file",
                "search_for_pattern",
                "get_symbols_overview",
                "find_symbol",
                "find_referencing_symbols",
                "find_implementations",
                "find_declaration",
                # Web / docs research
                "web_search",
                "fetch_url",
                "duckduckgo_search",
                "docs_search",
                "github_trending",
                "hacker_news",
                "reddit_posts",
                "twitter_search",
                "twitter_trending",
                "linkedin_jobs",
                "package_info",
                "oracle",
            }
        ),
    ),
    (
        "Edited",
        "✎",
        frozenset(
            {
                # Filesystem writes (deepagents built-ins + aliases)
                "write_file",
                "edit_file",
                "create_file",
                "multi_edit",
                "str_replace",
                "apply_patch",
                "delete",
                # Symbol-level edits
                "rename_symbol",
                "replace_symbol_body",
                "insert_after_symbol",
                "insert_before_symbol",
                "replace_content",
                "replace_in_files",
                "create_text_file",
                "safe_delete_symbol",
            }
        ),
    ),
    (
        "Ran",
        "$",
        frozenset(
            {
                "execute",
                "shell",
                "bash",
                "execute_bash",
                "run_command",
                "run_tests",
                "start_dev_server",
                "python_kernel",
                "daemon",
                "kill_process",
                "restart_task",
                "terminate_task",
            }
        ),
    ),
    (
        "Delegated",
        "⟐",
        frozenset(
            {
                "task",
                "start_async_task",
                "check_async_task",
                "update_async_task",
                "cancel_async_task",
                "list_async_tasks",
                "send_agent_message",
                "read_agent_messages",
            }
        ),
    ),
    (
        "Planned",
        "☰",
        frozenset(
            {
                "write_todos",
                "enter_plan_mode",
                "exit_plan_mode",
                "ask_user_question",
                "plan",
                "think",
                "compact_conversation",
            }
        ),
    ),
    (
        "Remembered",
        "◈",
        frozenset(
            {
                "remember",
                "recall",
                "forget",
                "write_memory",
                "read_memory",
                "list_memories",
                "create_memory_structure",
                "wiki_write",
                "wiki_read",
                "wiki_search",
                "wiki_update_index",
            }
        ),
    ),
    (
        "Produced",
        "◆",
        frozenset(
            {
                "create_artifact",
                "update_artifact",
                "list_artifacts",
                "skill_manage",
                "speak",
            }
        ),
    ),
    (
        "Tasks",
        "●",
        frozenset(
            {
                "get_task_status",
                "get_task_logs",
                "list_background_tasks",
            }
        ),
    ),
)

#: Category name → (glyph, member tool names), for O(1) lookup.
_TOOL_CATEGORY_INDEX: dict[str, tuple[str, frozenset[str]]] = {
    name: (glyph, members) for name, glyph, members in _TOOL_CATEGORIES
}

#: Fallback category for a tool not named in any group above.
_OTHER_CATEGORY = "Other"

#: MCP tools arrive as "<server>_<tool>" (langchain-mcp-adapters prefixes the
#: server name). These server prefixes route them into a sensible category
#: instead of "Other", so a Playwright or Serena burst reads as one section.
_MCP_SERVER_CATEGORY: tuple[tuple[str, str], ...] = (
    ("playwright", "Explored"),
    ("chrome-devtools", "Explored"),
    ("cua-driver", "Ran"),
    ("serena", "Explored"),
    ("apify", "Explored"),
    ("context7", "Explored"),
    ("langsmith", "Explored"),
)


def _tool_category(name: str) -> str:
    """The category heading a tool call belongs under (``"Other"`` if unknown)."""
    for cat_name, _glyph, members in _TOOL_CATEGORIES:
        if name in members:
            return cat_name
    # MCP tools: "<server>_<tool>" — match on the server prefix.
    lowered = name.lower()
    for prefix, cat in _MCP_SERVER_CATEGORY:
        if lowered.startswith(prefix):
            return cat
    return _OTHER_CATEGORY


def _category_glyph(cat: str) -> str:
    """The leading glyph for a category heading."""
    entry = _TOOL_CATEGORY_INDEX.get(cat)
    return entry[0] if entry else "•"


# The @mention token immediately before the cursor (start-of-line or after
# whitespace, so emails like user@host don't match). Used to drive @file/@agent
# autocomplete *anywhere* in the line, not just at the very start.
_AT_FRAGMENT_RE = re.compile(r"(?:^|(?<=\s))@([^\s@]*)$")

# Inputs that mean "quit", matched case-insensitively. Centralised because the
# check appears on four paths (dispatch, queued-command drain, slash handler,
# and the mid-turn bypass) and they must agree on what counts as exit.
_EXIT_COMMANDS = frozenset({"/quit", "/exit", "quit", "exit", "q"})


@dataclass(frozen=True)
class SlashCommand:
    """One TUI slash command. THE registry entry — autocomplete, dispatch, and
    /help are all derived from the TUI_COMMANDS table, so a command exists in
    exactly one place. (Previously each command lived in an autocomplete list,
    a ~50-branch elif chain, hand-written help text, and a passthrough set —
    and the four drifted.)

    handler: NovaApp method name, resolved with getattr at dispatch. Called
        with the full input text when ``wants_text``, else with no args; sync
        handlers and coroutines both work.
    """

    handler: str
    help: str
    wants_text: bool = True
    aliases: tuple[str, ...] = ()


# The table. Insertion order drives autocomplete + /help order.
# cron/webhook/prompt route through the legacy console handler via
# _passthrough_command (print-or-toggle-only commands; never read stdin or use
# a Live spinner — those would hang or garble inside Textual).
TUI_COMMANDS: dict[str, SlashCommand] = {
    "ui": SlashCommand(
        "_run_ui", "inspect / patch / preview / commit / rollback / reset UI panels"
    ),
    "help": SlashCommand("_run_help", "show this help", wants_text=False, aliases=("?",)),
    "init": SlashCommand("_run_init", "generate NOVA.md from the codebase"),
    "model": SlashCommand(
        "_run_model", "configure chat, subagent, and decision models", wants_text=False
    ),
    "router": SlashCommand(
        "_run_router", "switch routing setups and edit model routes", wants_text=False
    ),
    "auth": SlashCommand(
        "_run_auth",
        "manage API keys and provider sign-in",
        wants_text=False,
        aliases=("connect",),
    ),
    "sessions": SlashCommand("_run_session_import", "browse Nova and external sessions"),
    "import": SlashCommand(
        "_run_session_import", "import Codex / Claude / Nova history as context"
    ),
    "compare": SlashCommand(
        "_run_session_import", "compare two histories (/compare claude:<id> codex:<id>)"
    ),
    "tabs": SlashCommand(
        "_run_tabs_command",
        "manage parallel session tabs: launch / list / close (ctrl+n, alt+<n>)",
        aliases=("session",),
    ),
    "resume": SlashCommand("_run_resume", "resume a saved session for this path (/resume <id>)"),
    "artifacts": SlashCommand("_run_artifacts", "open the artifacts list", wants_text=False),
    "subagents": SlashCommand(
        "_run_subagents", "create/manage subagents (sync or async background)", wants_text=False
    ),
    "agent-server": SlashCommand("_run_agent_server", "local LangGraph server for subagents"),
    "tasks": SlashCommand("_run_tasks", "open the background tasks panel", wants_text=False),
    "cowork": SlashCommand(
        "_run_cowork", "launch the Nova Cowork desktop app (/cowork [task])", aliases=("desktop",)
    ),
    "mcp": SlashCommand("_run_mcp", "view / remove MCP servers", wants_text=False),
    "skills": SlashCommand(
        "_run_skills", "browse, pin, and manage the skill library", wants_text=False
    ),
    "agents": SlashCommand("_run_agents", "list subagents", wants_text=False),
    "plan": SlashCommand("_run_plan", "plan mode (status / off)"),
    "goal": SlashCommand("_run_goal", "set a persistent goal (status / clear)"),
    "btw": SlashCommand("_run_btw", "ask a side question without touching the main conversation"),
    "remote": SlashCommand("_run_remote", "manage Discord/Telegram bridges and response streaming"),
    "compact": SlashCommand("_run_compact", "summarize conversation to free context"),
    "update": SlashCommand("_run_update_check", "check/install updates and view release notes"),
    "save": SlashCommand("_run_save", "save the session now", wants_text=False),
    "copy": SlashCommand("_run_copy", "copy last response (or whole chat) — or click a message"),
    "plugins": SlashCommand("_run_plugins", "install / manage plugins and marketplaces"),
    "middleware": SlashCommand(
        "_run_middleware", "list active middleware (/reload-plugins to reload)", wants_text=False
    ),
    "reload-plugins": SlashCommand(
        "_run_reload_plugins",
        "reload plugin registrations",
        wants_text=False,
        aliases=("reload_plugins",),
    ),
    "steer": SlashCommand("_run_steer", "add/list/clear steering instructions"),
    "notifications": SlashCommand(
        "_run_notifications", "review/pending approvals (dismiss|approve <id> · clear)"
    ),
    "cron": SlashCommand("_passthrough_command", "manage scheduled (heartbeat) tasks"),
    "webhook": SlashCommand("_passthrough_command", "manage the webhook ingress server"),
    "prompt": SlashCommand("_passthrough_command", "manage evolving system-prompt templates"),
    "refine": SlashCommand(
        "_passthrough_command", "refinement audit trail (/refine history|rollback <id>)"
    ),
    "voice": SlashCommand("_run_voice", "local voice I/O settings (STT / VAD / TTS)"),
    "research": SlashCommand("_run_research", "launch a multi-agent research swarm"),
    "dream": SlashCommand("_run_dream", "reflect over memories to surface ideas", wants_text=False),
    "evolution": SlashCommand(
        "_run_evolution", "view skills unlocked / levelled up by complex tasks", wants_text=False
    ),
    "reindex": SlashCommand(
        "_run_reindex", "rebuild the semantic code-search index", wants_text=False
    ),
    "images": SlashCommand("_run_images", "list/remove/clear conversation images"),
    "files": SlashCommand("_run_files", "session file read/write summary", wants_text=False),
    "tests": SlashCommand("_run_tests", "run project tests (auto-detect or /tests <cmd>)"),
    "servers": SlashCommand("_run_servers", "manage dev servers", wants_text=False),
    "kill": SlashCommand("_run_kill", "kill processes"),
    "restore": SlashCommand("_run_restore", "restore a file from the snapshot trash"),
    "hooks": SlashCommand("_run_hooks", "list/enable/disable/remove hooks"),
    "browser-use": SlashCommand(
        "_run_browser_use", "AI browser automation, results analyzed by the agent"
    ),
    "ralph": SlashCommand("_run_ralph_screen", "autonomous looping mode (/ralph <task>)"),
    "trello": SlashCommand("_run_trello", "kanban task board in the browser"),
    "create": SlashCommand("_run_create", "Skills & Agents web UI"),
    "council": SlashCommand(
        "_run_council",
        "plan a task with the council (view / approve N / revise / history)",
    ),
    "clear": SlashCommand("_run_clear", "clear the transcript", wants_text=False),
    "tokens": SlashCommand("_run_token_view", "show token / context usage", wants_text=False),
    "context": SlashCommand("_run_context", "show context usage or manage imported context"),
    "cost": SlashCommand("_run_token_view", "show session token spend", wants_text=False),
    "verbose": SlashCommand("_run_verbose", "toggle internal-context display", wants_text=False),
    "trace": SlashCommand("_run_trace", "tracing status"),
    "log": SlashCommand("_run_log", "recent runs"),
    "theme": SlashCommand(
        "_run_theme", "switch color theme", wants_text=False, aliases=("themes",)
    ),
    "settings": SlashCommand("_run_settings", "open app settings", wants_text=False),
    "quit": SlashCommand("action_quit", "exit the TUI", wants_text=False),
    "exit": SlashCommand("action_quit", "exit the TUI", wants_text=False),
    # Wiki commands
    "ingest": SlashCommand("_run_ingest", "ingest a raw source into the wiki"),
    "ask": SlashCommand("_run_ask", "ask with wiki context prepended"),
    "file": SlashCommand("_run_file", "file conversation knowledge into the wiki"),
    "wiki": SlashCommand(
        "_run_wiki", "show Obsidian LLM Wiki browser (interactive)", wants_text=False
    ),
    "effort": SlashCommand("_run_effort", "set model reasoning effort"),
    "learning": SlashCommand(
        "_run_learning", "toggle Nova's autonomous learning loop (/learning on|off|status)"
    ),
}

_TUI_COMMAND_ALIASES: dict[str, str] = {
    alias: name for name, spec in TUI_COMMANDS.items() for alias in spec.aliases
}

# Commands that inspect or open UI controls can run alongside an agent turn.
# Commands that change the active agent, workspace, or turn input stay deferred.
_LIVE_UI_COMMANDS = frozenset(
    {
        "help",
        "evolution",
        "auth",
        "theme",
        "settings",
        "remote",
        "artifacts",
        "tasks",
        "mcp",
        "skills",
        "agents",
        "servers",
        "hooks",
        "tokens",
        "cost",
        "context",
        "trace",
        "log",
        "notifications",
    }
)

# Autocomplete entries — derived; plugin commands append at registration time.
_TUI_SLASH_COMMANDS = [f"/{name}" for name in TUI_COMMANDS]


def _read_tail(path: str, lines: int = 40) -> str:
    """The last *lines* of a file, for ``/agents logs``. Never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


from novacode_cli import ui_events as ev
from novacode_cli.agent_stream import run_agent_stream
from novacode_cli.config.config import console as _rich_console
from novacode_cli.input_utils import (
    PasteTracker,
    resolve_paste_placeholders,
)


def _status_for_event(event: Any, current: str) -> str:
    """Derive a session's tab status from the events already flowing.

    Cheaper than a side-channel status protocol, and it works identically for the
    in-process root session and a spawned child.
    """
    if isinstance(event, (ev.Done, ev.Cancelled, ev.Error)):
        return "idle"
    if isinstance(
        event, (ev.ToolCall, ev.AssistantMessage, ev.StatusUpdate, ev.TextDelta, ev.ReasoningDelta)
    ):
        return "running"
    return current


def _capture(fn, *args, **kwargs) -> Text:
    """Render an existing ``console.print``-based helper into a ``Text``.

    Lets the TUI reuse the legacy rich renderers (tool panels, todos, file ops)
    without printing to the real terminal — capture redirects the global
    console to an in-memory buffer.
    """
    with _rich_console.capture() as cap:
        fn(*args, **kwargs)
    return Text.from_ansi(cap.get())


def _approval_details(action_requests: list[dict]) -> Text:
    """Detailed, multi-line view of the actions awaiting approval."""
    from novacode_cli.ui.ui_elements import format_tool_display

    t = Text()
    t.append("⚠ The agent wants to run:\n\n", style="yellow")
    for ar in action_requests:
        name = ar.get("name", "?")
        args = ar.get("args", {}) or {}
        try:
            disp = format_tool_display(name, args)
        except Exception:  # noqa: BLE001
            disp = name
        t.append(f"  • {disp}\n", style="bold")
        if isinstance(args, dict):
            for k, v in args.items():
                sval = str(v).replace("\n", " ")
                if len(sval) > 160:
                    sval = sval[:160] + "…"
                t.append(f"      {k}: {sval}\n", style="dim")
    return t


#: Characters of in-progress prose kept in the live (pre-commit) preview.
_LIVE_PREVIEW_CHARS = 20_000

#: Percentage thresholds at which a usage meter turns amber, then red.
_PCT_WARN = 75
_PCT_CRITICAL = 90

#: Cap on the background-agent final answer echoed back to the main agent, so a
#: verbose run cannot flood the next turn's context.
_BG_REPORT_MAX_CHARS = 2000


def _pct_color(percent: float, base: str, pal: FooterPalette | None = None) -> str:
    """Recolor a usage percentage green→amber→red as it approaches the cap.

    Args:
        percent: The usage percentage (0-100).
        base: The color to use below the warning threshold.
        pal: Optional footer palette; when given, the warning/critical colours
            come from the active theme instead of the hardcoded fallbacks.

    Returns:
        A hex color string.
    """
    warn = pal.warning if pal is not None else "#e0af68"
    crit = pal.error if pal is not None else "#f7768e"
    if percent >= _PCT_CRITICAL:
        return crit
    if percent >= _PCT_WARN:
        return warn
    return base


def _active_palette() -> FooterPalette:
    """The active theme's footer palette, falling back to tokyo-night's.

    Only usable where a Textual app context exists; ``active_app`` is a
    ContextVar and is NOT visible inside a worker thread. Instance methods
    should prefer ``NovaApp._palette``, which reads the theme off ``self``.
    """
    from novacode_cli.tui.palette import cached_palette

    try:
        from textual.app import active_app

        app = active_app.get()
    except Exception:  # noqa: BLE001 — no app context (thread/import/test)
        app = None
    if app is None:
        return cached_palette(NOVA_TOKYO_NIGHT)
    try:
        return cached_palette(app.get_theme(app.theme))
    except Exception:  # noqa: BLE001 — a missing theme must not break the bar
        return cached_palette(NOVA_TOKYO_NIGHT)


def _render_bg_event(  # noqa: PLR0912, PLR0915 — one branch per event type is the point
    event: Any,  # noqa: ANN401 — event-stream dispatch, matches ui_events.py
    write: Callable[[str], None],
    set_phase: Callable[[str], None],
    fileop_summary: Callable[[Any], str],
    pending: dict[str, str],
) -> None:
    """Render one background-agent progress event into its card.

    The background (Ctrl+B) card is the only view of a detached agent turn, so it
    must explain *what* is happening, not just name the tool: tool arguments
    (``display_str``), the reasoning trace, results, file changes and subagent
    dispatches all go into the body, while ``set_phase`` keeps a one-line live
    status in the card title (visible even when the card is collapsed).

    A tool call and its result are rendered as ONE line (``● read_file(x) · Read
    42 lines``) rather than two. OutputLog cannot rewrite a line, so the call is
    buffered in ``pending`` and only written when its result arrives; a call with
    no result yet is flushed as-is by the caller when the turn ends.

    Args:
        event: A UI event from the agent stream.
        write: Sink for one line of Rich markup into the card body.
        set_phase: Sink for the live status line shown in the card title.
        fileop_summary: Summarizer for a file-op record (``+A / -D`` etc.).
        pending: Mutable buffer holding the in-flight tool call's rendered prefix,
            keyed by ``call_id`` (or ``""`` when the event carries no id).
    """
    from novacode_cli.ui_events import (
        AssistantMessage,
        FileOp,
        ReasoningDelta,
        StatusUpdate,
        SubagentActivity,
        TextDelta,
        ToolCall,
        ToolResult,
    )

    pal = _active_palette()

    def _flush_pending() -> None:
        """Write any buffered tool call that never got a result."""
        for prefix in pending.values():
            write(prefix)
        pending.clear()

    if isinstance(event, AssistantMessage) and event.text:
        _flush_pending()
        # Blank line before prose so it does not run into the tool lines above.
        write("")
        for line in event.text.splitlines():
            # Escape: prose may contain [brackets] (markdown links, list markers)
            # that Rich would otherwise interpret as markup.
            write(f"[white]{_esc(line)}[/white]" if line.strip() else "")
        set_phase("responding")
    elif isinstance(event, TextDelta) and event.text:
        # Deliberately NOT written. The committed AssistantMessage carries the
        # same text, and OutputLog is append-only (no replace), so rendering the
        # live preview here would duplicate every paragraph. The card title's
        # "responding" phase is the live signal instead.
        set_phase("responding")
    elif isinstance(event, ReasoningDelta) and event.text:
        # Dimmed thinking trace, so the user can see it is reasoning, not stalled.
        _flush_pending()
        # Blank line before the trace so it does not run into the tool lines above.
        write("")
        for line in event.text.splitlines():
            if line.strip():
                write(f"[dim italic]💭 {_esc(line)}[/dim italic]")
        set_phase("thinking")
    elif isinstance(event, StatusUpdate) and event.message:
        set_phase(event.message)
    elif isinstance(event, ToolCall):
        # Buffer the call; it is written together with its result so the pair
        # reads as one line. Show the tool AND its arguments (display_str).
        _flush_pending()
        pending[event.call_id or ""] = f"[cyan]{event.icon} {_esc(event.display_str)}[/cyan]"
        set_phase(f"running {event.name}…")
    elif isinstance(event, ToolResult):
        prefix = pending.pop(event.call_id or "", None)
        if prefix is None:
            # No matching call (e.g. a result for a call we never saw): stand alone.
            prefix = "[dim]·[/dim]"
        if event.is_error:
            write(
                f"[{pal.tool_fail}]✖[/{pal.tool_fail}] {prefix} [red]· {_esc(event.preview)}[/red]"
            )
        elif event.preview:
            write(f"[{pal.tool_ok}]●[/{pal.tool_ok}] {prefix} [dim]· {_esc(event.preview)}[/dim]")
        else:
            write(f"[{pal.tool_ok}]●[/{pal.tool_ok}] {prefix}")
    elif isinstance(event, FileOp):
        rec = event.record
        errored = bool(getattr(rec, "error", None)) or (getattr(rec, "status", "") == "error")
        mark = (
            f"[{pal.tool_fail}]✖[/{pal.tool_fail}]"
            if errored
            else f"[{pal.tool_ok}]●[/{pal.tool_ok}]"
        )
        prefix = pending.pop(event.call_id or "", None)
        if prefix is not None:
            write(f"{mark} {prefix} [dim]· {_esc(fileop_summary(rec))}[/dim]")
        else:
            write(f"{mark} [dim]{_esc(fileop_summary(rec))}[/dim]")
    elif isinstance(event, SubagentActivity):
        _flush_pending()
        who = _esc(event.subagent_type or "subagent")
        if event.kind == "dispatched":
            write(f"[magenta]◇ dispatched {who}[/magenta]")
        elif event.kind == "completed":
            write(f"[magenta]◆ {who} done[/magenta]")
        elif event.message:
            write(f"[magenta]◇ {_esc(event.message)}[/magenta]")


def _paint(widget: Any, content: Any) -> None:
    """``Static.update`` that re-lays-out only when the content's size changed.

    ``update()`` defaults to ``layout=True`` and a layout re-arranges the whole
    screen. For a clock or a counter the text changes every second but its
    width and line count almost never do — those frames only need a repaint.
    Identical content is skipped entirely.
    """
    if not isinstance(content, (str, Text)):
        # A Group/Markdown/Table: its size is not knowable from text, so take
        # the safe path.
        widget.update(content)
        widget._nova_painted = None
        return
    plain = content.plain if hasattr(content, "plain") else str(content)
    width = content.cell_len if hasattr(content, "cell_len") else len(plain)
    dims = (width, plain.count("\n"))
    # Text.__eq__ compares plain text and spans but NOT the base style, so a
    # label recoloured by a theme switch would look "identical" — include it.
    look = (plain, str(getattr(content, "style", "")), tuple(getattr(content, "spans", ())))
    prev = getattr(widget, "_nova_painted", None)
    if prev is not None and prev[0] == look:
        return
    widget.update(content, layout=prev is None or prev[1] != dims)
    widget._nova_painted = (look, dims)


#: Live tool output is shown in a pane a few lines tall; rendering megabytes of
#: it (highlighting every line) froze the UI for seconds per flush. Only the
#: tail of each batch is drawn — the full output still reaches the model.
_LIVE_OUTPUT_MAX_LINES = 200
_LIVE_OUTPUT_MAX_CHARS = 20_000
_LOG_MAX_LINES = 2_000
_SUBAGENT_LOG_MAX_ENTRIES = 300
_PENDING_JOB_NOTE_MAX = 100
#: Cap on a failed tool call's stored output, so one enormous traceback cannot
#: bloat the entry (and the group's repaint) without bound.
_TOOL_ERROR_MAX_CHARS = 6_000
#: How many lines of that output the group renders inline before summarising.
_TOOL_ERROR_MAX_LINES = 12
#: Saved messages drawn on resume (the agent still gets the full history).
_REPLAY_MAX_MESSAGES = 150


def _display_tail(text: str) -> str:
    """The part of a live-output batch worth rendering."""
    if len(text) <= _LIVE_OUTPUT_MAX_CHARS and text.count("\n") <= _LIVE_OUTPUT_MAX_LINES:
        return text
    lines = text[-_LIVE_OUTPUT_MAX_CHARS:].splitlines(keepends=True)[-_LIVE_OUTPUT_MAX_LINES:]
    return "… output trimmed (truncated), showing the tail …\n" + "".join(lines)


def _retitle(group: Any, title: Any) -> None:
    """Set a Collapsible's title, skipping the layout pass when its width is unchanged.

    ``Collapsible.title = x`` ends in ``Static.update(layout=True)``, which
    re-arranges the whole screen. The animated tool-group header changes colour
    every frame but almost never changes width, so it only needs a repaint.
    Falls back to the plain assignment for anything unexpected.
    """
    from textual.content import Content

    head = getattr(group, "_title", None)
    try:
        new = Content.from_text(title)
        old = head.label
        if head is None or new.cell_length != old.cell_length:
            raise ValueError  # width changed (or no title widget): real layout
        head.set_reactive(type(head).label, new)  # no watcher -> no layout
        symbol = head.collapsed_symbol if head.collapsed else head.expanded_symbol
        head.update(Content.assemble(symbol, " ", new), layout=False)
    except Exception:  # noqa: BLE001
        group.title = title


class DesktopNotificationClicked(Message):
    """A click identifies a session and notification; it never grants approval."""

    def __init__(self, sid: str, notification_id: str):
        super().__init__()
        self.sid = sid
        self.notification_id = notification_id


class NovaApp(App):
    """Phase-1 Nova chat TUI."""

    # Colors come from the active Textual theme (default: tokyo-night, registered
    # in on_mount). Do NOT redefine $primary/$surface/etc. here — CSS variable
    # definitions override the theme and break /theme switching.
    CSS = """
    /* --- App chrome --- */
    Screen { background: $background; }

    /* Scrollbars are hidden cosmetically across the app (transcript, embedded
       terminal, tool/subagent logs, modals, lists). Scrolling still works via
       the wheel, keys, and drag; only the bar is not painted.
       A transparent scrollbar color makes ScrollBar.render() emit blanks while
       keeping the bar's size, so layout is untouched. Do NOT instead set
       `scrollbar-size-vertical: 0`: a zero scrollbar size on a scroll container
       breaks Textual's content-width calculation and collapses its children to
       zero width (verified: the transcript's message bodies went 69 -> 0 cols).
       These must be set on the SCROLL CONTAINERS, not on Screen: ScrollBar
       reads `self.parent.styles.scrollbar_background`, so a Screen-level rule
       never reaches the bar and it still paints (as a black column).
       The `scrollbar-gutter: stable` rules below keep the reserved column so
       content width does not shift when a scrollable region appears. */
    VerticalScroll, OutputLog, OptionList, TextArea, Collapsible, SelectionList,
    Select, DataTable, Log, Markdown, Tree, DirectoryTree, TabbedContent {
        scrollbar-color: transparent;
        scrollbar-background: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-color-active: transparent;
        scrollbar-background-active: transparent;
    }

    /* Session panes. The ContentSwitcher wrapping the transcript must be fully
       transparent to layout: without these it defaults to auto sizing, the
       transcript stops filling the screen and its content width shrinks, so the
       Matrix-rain banner (sized from the TERMINAL width) no longer fits its
       container and every row wraps — which pushed the logo down a row and then
       back up as the rain shifted. Panes are styled as a group so a spawned
       session's transcript looks identical to the root one. */
    #workspace-layout { height: 1fr; width: 100%; }
    #panes { height: 1fr; width: 1fr; }
    #panes > VerticalScroll { height: 1fr; width: 100%; padding: 1 2; }
    /* Hidden until a second session exists; _refresh_tabs() toggles it.
       Height MUST be explicit: the Tabs widget's inner tabs-scroll is height:1fr,
       and a 1fr child inside an `auto` parent expands to the whole screen — the
       tab bar then ate ~30 rows and crushed #panes (every session pane, root
       included) to a 1-row sliver, so nothing rendered once the bar appeared. */
    #session-tabs { display: none; height: 3; width: 100%; }
    #transcript { height: 1fr; padding: 1 2; }
    #transcript > .subagent {
        border-left: thick $accent; padding: 0 2;
        margin: 1 0; background: $surface;
    }
    #transcript > .tool { color: $warning; padding: 0 2; margin: 1 0; background: $background; }
    .toolbody { color: $text-muted; margin: 0; height: auto; }
    .terminal-log {
        /* Scale with terminal height; floor at the old fixed 5 rows so small
           windows are never worse, grow up to 16 on tall windows. */
        height: 25vh;
        min-height: 5;
        max-height: 16;
        background: $boost;
        border: round $border;
        margin: 0 0;
        padding: 0 1;
        scrollbar-gutter: stable;
    }
    #tool-group-log, #subagent-log {
        display: none;
    }
    #tool-group-log.active, #subagent-log.active {
        display: block;
    }
    #transcript > .logline {
        height: auto; padding: 0 2;
        background: $surface; margin: 1 0;
    }
    /* --- /ralph native cards (accent-bar style, like .initlog) --- */
    #transcript > .ralph-run {
        height: auto; border-left: thick $primary;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .ralph-iter {
        height: auto; border-left: thick $accent;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .ralph-iter.done { border-left: thick $success; }
    #transcript > .ralph-iter.failed { border-left: thick $error; }
    #transcript > .ralph-summary {
        height: auto; border-left: thick $success;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .ralph-status {
        height: auto; border-left: thick $secondary;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .nova-event {
        height: auto; padding: 0 2;
        background: $surface; margin: 1 0;
        border-left: thick $accent;
    }
    /* Theme variables, not hardcoded hex: these bars must recolor with /theme
       like every other accent bar. */
    #transcript > .nova-event.nova-review-start {
        border-left: thick $primary;
    }
    #transcript > .nova-event.nova-review-complete {
        border-left: thick $success;
    }
    #transcript > .nova-event.nova-skill-refinement {
        border-left: thick $warning;
    }
    #cmdpalette {
        width: 100%;
        /* Grow the completion list on taller terminals (was a fixed 10 rows). */
        height: auto; max-height: 40vh;
        border: thick $accent; background: $panel;
        padding: 0 1;
        display: none; layer: overlay; dock: bottom;
        margin-bottom: 8;
    }
    /* --- Prompt dock: 3-row bottom section --- */
    #prompt-dock {
        dock: bottom;
        height: auto;
        background: $surface;
    }
    /* --- Status row: activity + context meter on the left, counts flush right.
       The 1fr/auto split is load-bearing: it is the only way a Text can be
       right-aligned here (Rich Text has no right-align of its own). --- */
    #status-row {
        height: 1;
        background: $background;
        padding: 0 2;
    }
    #prompt-hint-bar {
        width: 1fr;
        height: 1;
        background: $background;
        color: $text-muted;
        /* Truncate at the edge rather than colliding with the right-docked
           counts: the status Text is long and its right end must yield. */
        overflow-x: hidden;
        text-overflow: ellipsis;
    }
    #status-counts {
        width: auto;
        height: 1;
        background: $background;
        color: $text-muted;
        /* A guaranteed gap from the (possibly truncated) status text, so the
           two never read as one run-on line on a mid-width terminal. */
        padding: 0 0 0 2;
    }
    #prompt {
        width: 1fr;
        background: $panel; color: $text;
        padding: 0 2;
        /* Grows with the text (multi-line composing, wrapped long lines)
           and stops before it eats the transcript. */
        height: auto; min-height: 3; max-height: 15;
        border: none;
        /* Smooth fade when switching into/out of a mode. */
        transition: background 300ms in_out_cubic;
    }
    #prompt:focus {
        background: $boost;
    }
    /* The > chevron prefix for the input. */
    #prompt-prefix {
        width: 3;
        height: 3;
        padding: 0 0 0 1;
        background: $panel;
        color: $accent;
    }
    #prompt-row {
        height: auto;
        background: $panel;
        padding: 0;
        /* `solid` (not `tall`): a tall border is drawn with half-block glyphs,
           which render as a noisy dotted line rather than a hairline. */
        border-top: solid $border 35%;
        border-bottom: solid $border 35%;
    }
    /* BASH mode — magenta, urgent. Theme tokens, so the mode colours follow
       /theme like everything else (they were hardcoded tokyo-night hexes). */
    #prompt.bash-mode {
        background: $accent 12%; color: $text;
    }
    #prompt:focus.bash-mode {
        background: $accent 20%;
    }
    #prompt-row.bash-mode {
        border-top: solid $accent 60%;
        border-bottom: solid $accent 60%;
    }
    #prompt-prefix.bash-mode { color: $accent; background: $accent 12%; }
    /* PLAN mode — blue, calm. */
    #prompt.plan-mode {
        background: $primary 14%; color: $text;
    }
    #prompt:focus.plan-mode {
        background: $primary 22%;
    }
    #prompt-row.plan-mode {
        border-top: solid $primary 60%;
        border-bottom: solid $primary 60%;
    }
    #prompt-prefix.plan-mode { color: $primary; background: $primary 14%; }
    /* Input-mode badge (plan / bash / goal). Sits between the input and the
       info bar; empty in normal mode. Colours come from the theme so the modes
       track /theme instead of carrying tokyo-night hexes. */
    #mode-badge {
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text-muted;
    }
    /* --- Info bar: workspace / branch / sandbox / model / context / artifacts.
       Two decks: a micro-caps label row over a value row, so the values line up
       in columns instead of drifting with each label's width. */
    #info-bar {
        height: 2;
        padding: 0 1;
        background: $background;
    }
    /* Docked todo checklist: sits above the prompt so it stays on screen.
       Auto-height so a short list costs a few rows; max-height clamps a
       long one (real lists run 2-14 items) and scrolls inside itself. */
    /* Inside #prompt-dock (a Vertical), so it takes its own rows above the
       input instead of fighting it for the same bottom-docked rows. */
    #todo-scroll {
        display: none;
        height: auto;
        max-height: 12;
        overflow-x: hidden;
        overflow-y: auto;
    }
    #todo-scroll.active { display: block; }
    #todo-scroll.collapsed { max-height: 1; overflow-y: hidden; }
    #todo-dock {
        display: none;
        height: auto;
        padding: 0 2;
        background: $surface;
        /* No accent bar: the checklist reads as plain transcript chrome. The
           "Todos" word in the header carries the theme color instead. */
    }
    #todo-dock.active { display: block; }
    #todo-dock:hover { background: $boost; }
    /* Collapsed: just the one-line summary header. */
    #todo-dock.collapsed { max-height: 1; overflow-y: hidden; }
    /* --- Dynamic subagents: the fan-out a dispatch spawned, while it runs.
       Above the transcript so a 32-way fan-out stays watchable, and hidden
       entirely until something is in flight. The collapsed state keeps the one
       header line, which is where the counts and the hint live. --- */
    #subagents-dock {
        display: none;
        height: auto;
        max-height: 14;
        overflow-y: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $border;
    }
    #subagents-title { color: $accent; text-style: bold; }
    #subagents-dock.active { display: block; }
    #subagents-dock:hover { background: $boost; }
    #subagents-dock.collapsed #subagents-body { display: none; }
    /* --- Question dock: the ask_user_question answer list, docked above the
       input rather than pushed as a modal, so the transcript stays visible and
       the footer keeps one layout. Same show/hide shape as #todo-dock.
       Colour comes from classes, not markup: the option rows are plain Static
       children, so an option containing '[' cannot be parsed as markup. --- */
    #question-dock {
        display: none;
        height: auto;
        padding: 0 2;
        background: $surface;
    }
    #question-dock.active { display: block; }
    #question-context {
        height: auto;
        color: $text-muted;
    }
    #question-text {
        height: auto;
        color: $text;
        padding-bottom: 1;
    }
    /* Clamped so a long question cannot eat the transcript; scrolls inside. */
    #question-options {
        height: auto;
        max-height: 14;
        overflow-y: auto;
    }
    #question-options.hidden { display: none; }
    .question-option { height: auto; }
    /* Selected row: a dark band plus the accent label. No border — the row
       highlight and the colour are the whole affordance.
       $panel, not $boost: $boost resolves to transparent in this app's theme
       (measured #00000000), so a $boost band would be invisible. $panel is the
       next step up from the dock's $surface. */
    .question-option.selected { background: $panel; }
    .question-option .q-label { color: $text; }
    .question-option.selected .q-label {
        color: $accent;
        text-style: bold;
    }
    .question-option .q-desc {
        color: $text-muted;
        padding-left: 4;
    }
    #question-input { display: none; }
    #question-input.active { display: block; }
    /* The legend carries its own two tones via Content spans, so no colour rule
       here — a color: would be overridden by the inline styles anyway. */
    #question-hints {
        height: 1;
    }
    #tasks-bar {
        display: none;
        height: 1;
        padding: 0 1;
        background: $background;
        color: $accent;
    }
    #tasks-bar.active { display: block; }
    /* "Jump to latest" — shown only while the transcript is scrolled away from
       the bottom, so a long scrollback never traps the user. Sits at the
       top-right of the footer, directly above the status/skills bar.
       The row is transparent and right-aligns its child; the child shrinks to
       its text (width: auto), so ONLY the words are a click target — the empty
       space to their left is not part of the button. */
    #jump-latest-row {
        display: none;
        height: 1;
        align: right middle;
        background: transparent;
    }
    #jump-latest-row.active { display: block; }
    /* Shrink to the text: the box is only as wide as the label, so the row is
       not a full-width bar. */
    #jump-latest-box {
        width: auto;
        height: 1;
        background: transparent;
    }
    #jump-latest {
        width: auto;
        height: 1;
        min-height: 1;
        min-width: 0;
        border: none;
        padding: 0 2;
        background: transparent;
        color: $accent;
    }
    #jump-latest:hover { color: $foreground; text-style: bold; }
    .info-col {
        height: 2;
        padding: 0 1;
        width: 1fr;
    }
    /* Micro-caps labels: small, letter-spaced, dim — they read as field names
       rather than as content, so the bold value row below carries the eye. */
    .info-label {
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    /* The accent tick on the first field only: it anchors the start of the
       footer row without turning every label into a competing accent. */
    .info-label.first {
        color: $accent;
    }
    .info-value {
        height: 1;
        text-style: bold;
        /* Ellipsis, not the default `fold`: a folded value wraps onto a second
           row and pushes the value deck out of the 1-row box it must occupy. */
        overflow-x: hidden;
        text-overflow: ellipsis;
    }
    /* Narrow terminals: shed the widest info columns so the rest stay readable
       instead of being squeezed to a few clipped characters. Toggled by
       _apply_responsive_layout adding the `narrow` class to the screen. */
    .narrow #col-workspace, .narrow #col-sandbox { display: none; }
    .compact #col-branch, .compact #col-artifacts { display: none; }
    .tiny #info-bar { display: none; }
    .short #info-bar { display: none; }
    .short #prompt { max-height: 5; }
    .short #todo-scroll { max-height: 4; }
    .short #subagents-dock { max-height: 6; }
    .short #question-options { max-height: 5; }
    .short #cmdpalette { max-height: 30vh; margin-bottom: 3; }
    .short #model-options { max-height: 6; }
    .short #model-decisions { max-height: 7; }
    .tiny #transcript { padding: 0 1; }
    .tiny #prompt { padding: 0 1; max-height: 3; }
    .tiny #prompt-prefix { width: 2; }
    ModalScreen.narrow #modal-box, ModalScreen.compact #modal-box {
        width: 96%; max-width: 110; padding: 1 2;
    }
    ModalScreen.short #modal-box { max-height: 96%; padding: 0 2; }
    .narrow #modal-buttons, .short #modal-buttons {
        layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 0 1;
    }
    .narrow #modal-buttons Button, .short #modal-buttons Button {
        width: 1fr; margin: 0;
    }
    .short .preview-box { max-height: 4; }
    .short #sessions, .short #pick-list, .short #infolist,
    .short #mcp-configured, .short #mcp-presets, .short #plugins,
    .short #cplugins-list, .short #agents-list, .short #servers-list,
    .short #hooks-list, .short #wiki-pages-list, .short #wiki-inbox-list,
    .short #tasks-list {
        max-height: 25%;
    }
    .session-header {
        height: auto;
        padding: 1 2;
        background: $surface;
        align: left middle;
    }
    .session-pill {
        background: $boost;
        border: round $border;
        padding: 0 2;
        margin: 0 1;
        height: auto;
    }
    .pill-model { color: $primary; }
    .pill-sandbox { color: $success; }
    .pill-memory { color: $accent; }
    .breadcrumb { color: $text-muted; }
    Screen > .modal-backdrop { background: $surface 50%; }
    /* Every modal centers its box and dims the backdrop. */
    ModalScreen { align: center middle; background: $surface 50%; }
    /* Approval body scrolls within bounds so the choices never get clipped. */
    #modal-body-scroll { height: auto; max-height: 55%; scrollbar-gutter: stable; }
    #choices {
        height: auto; max-height: 8; margin-top: 1;
        border: round $accent; background: $boost;
    }
    #choices:focus { border: round $warning; }
    #modal-box {
        width: 80%; max-width: 110; height: auto; max-height: 90%;
        border: thick $accent; background: $surface;
        padding: 1 4; layer: overlay;
    }
    #modal-title { margin-bottom: 1; padding: 0 0; }
    /* The live task list owns the space left by its header and hints. */
    BackgroundTasksScreen #modal-box {
        width: 94%; height: 90%; max-height: 96%; padding: 1 2;
    }
    BackgroundTasksScreen.short #modal-box, BackgroundTasksScreen.tasks-short #modal-box {
        height: 96%; padding: 0 1;
    }
    BackgroundTasksScreen #modal-title { height: auto; margin-bottom: 0; }
    BackgroundTasksScreen #tasks-list, BackgroundTasksScreen.short #tasks-list {
        height: 1fr; min-height: 3; max-height: 100%; padding: 0 1;
        overflow-y: auto; scrollbar-gutter: stable;
    }
    BackgroundTasksScreen #tasks-hint { height: auto; max-height: 3; }
    BackgroundTasksScreen.tasks-short #tasks-hint { max-height: 1; }
    #modal-body { padding: 0 0; }
    /* Long lists scroll inside the box instead of overflowing the screen. */
    #sessions, #pick-list, #infolist, #mcp-configured, #mcp-presets, #plugins, #cplugins-list, #agents-list, #skills-list, #servers-list, #hooks-list, #wiki-pages-list, #wiki-inbox-list, #tasks-list {
        height: auto; max-height: 40%;
        padding: 0 2;
    }
    #wiki-tab-buttons {
        height: auto;
        margin-bottom: 1;
    }
    #wiki-tab-buttons Button {
        margin-right: 1;
    }
    #pages-container, #inbox-container {
        height: auto;
    }
    #pages-header, #inbox-header {
        margin-bottom: 0;
    }
    .preview-box {
        background: $boost;
        border: round $accent 50%;
        padding: 1 2;
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        max-height: 12;
        scrollbar-gutter: stable;
        overflow-y: scroll;
    }
    /* The /model picker list sits ABOVE the free-text field and the
       Switch/Cancel buttons, so it gets a bounded height and its own scroll —
       otherwise a long list pushes the buttons out of the modal and they can't
       be clicked. Kept in sync with the reference layout's 16-row cap. */
    /* Height MUST be explicit, for the reason spelled out on #session-tabs above:
       the Tabs widget's inner tabs-scroll is height:1fr, and a 1fr child inside
       this box's `height: auto` grows to the whole box. Measured: the bar took 32
       of the modal's 36 rows and pushed the filter, the list, the free-text field
       and the Switch/Cancel buttons clean out of view, so /model rendered as a
       titled empty frame with a working-looking tab bar at the top of it. */
    #model-tabs { height: 2; width: 100%; margin-bottom: 1; }
    #model-options {
        /* 16 was the cap while the list was the only thing above the free-text
           field; the tab bar spends 3 rows of the same budget (2 + margin), so it
           drops to 13 rather than pushing Switch/Cancel past the bottom of a
           short terminal. Measured at 120x40: at 16 the buttons were clipped by
           the box's `max-height: 90%`, at 13 they are inside it. */
        height: auto; max-height: 13;
        border: round $accent 50%; margin-bottom: 1;
        scrollbar-gutter: stable;
    }
    #model-filter { margin-bottom: 1; }
    #model-custom-row { height: auto; }
    #model-provider { width: 28; }
    #model-custom-row > #model { width: 1fr; }
    #modelinfo { height: auto; color: $text-muted; margin-bottom: 1; }
    #model-hint { padding: 0 1; color: $text-muted; }
    /* /auth: the provider list must scroll rather than push the buttons off a
       short terminal. */
    #auth-list {
        height: auto; max-height: 16;
        border: round $accent 50%; margin-bottom: 1;
        scrollbar-gutter: stable;
    }
    #auth-desc { height: auto; color: $text-muted; margin-bottom: 1; }
    #auth-hint { padding: 0 1; color: $text-muted; }
    #auth-error { padding: 0 1; height: auto; }
    #auth-endpoint-label { margin-top: 1; }
    #modal-buttons {
        height: auto; align: center middle;
        margin-top: 1; padding: 0 0;
    }
    #modal-buttons Button { margin: 0 1; }
    #modal-hint { padding: 0 1; color: $text-muted; }
    Collapsible { margin: 0; }
    Collapsible > .collapsible--title { padding: 0 1; background: $surface; }
    /* The condensed tool group reads as plain transcript, not a raised card:
       its background matches the screen's, and it must not shift color on hover
       or when focus lands inside it (Collapsible's default :focus-within tint,
       and CollapsibleTitle's own :hover/:focus backgrounds). */
    #transcript > .tool, #transcript > .tool > CollapsibleTitle {
        background: $background;
    }
    #transcript > .tool:hover, #transcript > .tool:focus-within {
        background: $background;
        background-tint: transparent;
    }
    #transcript > .tool > CollapsibleTitle:hover,
    #transcript > .tool > CollapsibleTitle:focus {
        background: $background;
        color: $warning;
    }
    .btw-card { margin: 1 0; border-left: thick $accent-muted; }
    .btw-card > .collapsible--title { color: $accent-muted; background: $surface; }
    .btw-body { padding: 0 2; color: $text-muted; }
    /* Compaction is housekeeping, not conversation: a quiet one-line card that
       expands for the stats and summary. Muted so it recedes in the transcript. */
    .compact-card { margin: 1 0; border-left: thick $success-muted; }
    .compact-card > .collapsible--title {
        color: $success-muted;
        background: $surface;
    }
    .compact-body { padding: 0 2; color: $text-muted; }
    .bgshell-card { margin: 1 0; border-left: thick $warning-muted; }
    .bgshell-card > .collapsible--title { color: $warning; background: $surface; }
    /* Sized to its output, like the agent card: a command that prints nothing
       (`code .`) used to reserve 30vh for an empty log, inside a body that
       stretched the card to the full transcript height. */
    .bgshell-log {
        height: auto; max-height: 22;
        border: none; background: $surface;
        padding: 0 1;
    }
    .bgshell-card Contents > Vertical { height: auto; }
    /* A plain `!cmd`: no card, just the command on a highlighted row and its
       output indented beneath, the way a shell transcript reads. */
    .bash-inline { height: auto; margin: 1 0 0 0; }
    .bash-inline-head { width: 1fr; background: $panel; }
    .bash-inline-log {
        height: auto; max-height: 22;
        border: none; background: transparent; padding: 0;
    }
    /* The agent card emits discrete progress lines, not a firehose of command
       output, so it sizes to its content instead of reserving 30vh. Capped so a
       long run cannot swallow the transcript. */
    .bgagent-card {
        margin: 1 0;
        border-left: thick $success-muted;
        background: $surface;
    }
    .bgagent-card > .collapsible--title {
        color: $success;
        background: $surface;
        text-style: bold;
    }
    .bgagent-log {
        height: auto; max-height: 14;
        border: none; background: $surface;
        padding: 0 1;
    }
    /* The Collapsible body is a Vertical that defaults to 1fr, which would
       stretch the card to the full transcript height. Size it to the log.
       (The Vertical sits inside the Collapsible's Contents wrapper.) */
    .bgagent-card Contents > Vertical { height: auto; }
    .bgagent-done > .collapsible--title { color: $success; }
    .bgagent-failed > .collapsible--title { color: $error; }
    /* No reserved scrollbar column: the bar is hidden (see the transparent
       scrollbar colors above), so keeping the gutter would waste 2 columns and
       stop the home banner's rain from reaching the right edge. */
    VerticalScroll { scrollbar-gutter: auto; }
    #remote-status-container {
        height: auto; max-height: 12;
        background: $boost;
        border: round $accent;
        padding: 0 1;
        margin-bottom: 1;
    }
    #remote-section-title {
        margin-top: 1;
        margin-bottom: 0;
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("ctrl+shift+backspace", "ui_reset", "Restore UI", priority=True),
        *[
            Binding(f"f{i}", f"harness_key('f{i}')", show=False, priority=True)
            for i in range(6, 13)
        ],
        ("ctrl+q", "quit", "Quit"),
        # ctrl+c copies the current text selection if there is one, else quits.
        # Textual captures the mouse, so the terminal's native copy doesn't work
        # in the transcript — this restores select-then-copy while keeping the
        # familiar ctrl+c-to-quit when nothing is selected. ctrl+q always quits.
        ("ctrl+c", "copy_or_quit", "Copy / Quit"),
        ("ctrl+t", "toggle_terminal", "Terminal"),
        Binding("ctrl+b", "run_background", "Background", priority=True),
        ("ctrl+g", "voice_talk", "Talk"),
        ("ctrl+l", "voice_toggle", "Listen"),
        ("escape", "cancel_turn", "Cancel"),
        # Parallel sessions. alt+… chords are used because ctrl+a/e/k/u/w are
        # shadowed by Textual's Input editing bindings while #prompt has focus
        # (the normal state).
        ("alt+t", "toggle_todos", "Todos"),
        ("alt+s", "toggle_subagents", "Subagents"),
        ("ctrl+end", "jump_latest", "Jump to latest"),
        ("ctrl+n", "new_session", "New session"),
        ("alt+right", "next_session", "Next session"),
        ("alt+left", "prev_session", "Prev session"),
        *[(f"alt+{i}", f"goto_session({i})", f"Session {i}") for i in range(1, 10)],
    ]

    def __init__(
        self,
        *,
        agent,
        assistant_id,
        session_state,
        backend,
        token_tracker,
        image_tracker,
        model_name,
        model_provider: str | None = None,
        session_manager=None,
        restored_messages=None,
        sandbox_id: str | None = None,
        sandbox_type: str | None = None,
        sandbox_meta: dict | None = None,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.assistant_id = assistant_id
        self._question_future: asyncio.Future | None = None
        self._submit_on_enter = True
        self._autocomplete_enabled = True
        self._low_resource_mode = False
        from novacode_cli.tui.animation_rate import DEFAULT_ANIMATION_FPS

        self._animation_fps = DEFAULT_ANIMATION_FPS
        self._animate_matrix_rain = False
        self.session_state = session_state
        self.backend = backend
        self.token_tracker = token_tracker
        self.image_tracker = image_tracker
        # Collapses large pastes into [paste #N +M lines] placeholders; resolved
        # back to full text on submit. Shared helpers with the legacy input.
        self.paste_tracker = PasteTracker()
        self.model_name = model_name or "unknown"
        self._routed_model_name: str | None = None
        try:
            from novacode_cli.config.nova_config import NovaConfig

            self._router_mode_enabled = NovaConfig().get_router_enabled()
        except Exception:  # noqa: BLE001 — the footer must not block app startup
            self._router_mode_enabled = False
        # Provider of the live model, recorded on save so a resume can rebuild
        # the exact model instead of falling back to the global config. Derived
        # from the same precedence chain that built the model; kept in sync by
        # the model-switch path. None means "unknown", which resume treats as
        # "nothing to restore".
        #
        # A RESUMED session passes its own provider in: the global config still
        # describes whatever was last selected, which need not be this session's
        # model. Deriving from the global config here would then re-record the
        # wrong provider on the very next save and quietly lose the session's
        # model for good.
        if model_provider:
            self._model_provider: str | None = model_provider
        else:
            try:
                from novacode_cli.utils.model_info import get_current_provider

                self._model_provider = get_current_provider()
            except Exception:  # noqa: BLE001 — never block app construction
                self._model_provider = None
        self.session_manager = session_manager
        self._session_save_lock = asyncio.Lock()
        self._autosave_running = False
        # Sandbox identity for session persistence (so --continue can reconnect).
        self._sandbox_id = sandbox_id
        self._sandbox_type = sandbox_type
        self._sandbox_meta = sandbox_meta
        # Prior conversation turns to replay into the transcript on resume.
        self._restored_messages = list(restored_messages or [])
        self._replaying = False  # True while _replay_history renders
        self._seen: set[str] = set()
        self._speech_lock = asyncio.Lock()
        # Ropes, not plain strings: both accumulate one fragment per model delta,
        # and an attribute-level `+=` is quadratic (see the _live_buf property).
        self._live_buf_parts: list[str] = []
        self._reasoning_buf_parts: list[str] = []
        self._stream_msg: ChatMessage | None = None  # in-progress Nova answer widget
        self._reason_msg: ChatMessage | None = None  # in-progress reasoning widget
        self._current_assistant_id: str | None = None
        # Streaming coalescing: deltas append to the buffers above, but the widget
        # is only repainted on a 100ms timer (see _schedule_stream_flush) so a fast
        # token stream doesn't trigger a full re-render + scroll per token. The
        # first fragment of a stream paints immediately instead of waiting it out.
        self._stream_flush_scheduled = False
        # Transcript pruning is coalesced onto a zero-delay timer: a burst of
        # mounts/log lines produces ONE prune instead of one per widget (see
        # _schedule_prune).
        self._prune_scheduled = False
        # "Follow the tail" intent: True while the user wants new content to
        # auto-scroll into view. It is NOT the same as being geometrically at the
        # bottom — content growth raises max_scroll_y without the user moving, so
        # geometry alone cannot tell "the user scrolled away" from "the answer got
        # longer". Only a scroll that moves the viewport *up* clears this flag;
        # reaching the bottom (or clicking jump-to-latest) sets it again.
        self._follow_tail = True
        # Last label rendered into #jump-latest. _update_jump_latest runs on every
        # scroll event and every ~100ms streaming flush, so re-rendering the same
        # text would churn the footer layout ~10x/sec while thinking streams.
        self._jump_latest_label = ""
        # Cached singleton widget refs (resolved once in on_mount) to avoid a
        # query_one DOM walk on every delta / keystroke / status tick.
        self._w_cache: dict[str, Any] = {}
        # call_id -> (collapsible, body Static, base title) for open tool calls
        self._tool_components: dict[str, tuple[Collapsible, Static, str]] = {}
        # fallback for tool calls that arrive without an id
        self._last_tool: tuple[Collapsible, Static, str] | None = None
        # Condensed tool view: a run of consecutive tool calls collapses into a
        # single "tool group" panel (one compact line per tool) instead of one
        # Collapsible per call, so a burst of tools doesn't flood the chat.
        self._tool_group: Collapsible | None = None
        self._tool_group_body: Vertical | None = None
        self._tool_group_entries: list[dict] = []  # per-tool {base, mark, detail, error}
        self._tool_group_lines: dict[str, int] = {}  # call_id -> entry index
        self._tool_group_last_idx: int | None = None  # fallback for id-less results
        # Coalescing state for tool-group repaints (see
        # _schedule_tool_group_refresh): a burst of tool events paints once.
        self._tool_group_refresh_scheduled: bool = False
        self._tool_group_running: str | None = None
        # subagent tracking: call_id -> (collapsible, body Static, type, start_time)
        self._subagent_widgets: dict[str, tuple[Collapsible, Static, str, float]] = {}
        self._subagent_count: int = 0  # running total for display
        # Maps running subagent tool call_id -> subagent task call_id
        self._subagent_tool_to_task: dict[str, str] = {}
        self._remote_msg: Any = None  # current RemoteMessage during remote turn
        self._remote_launch_approvals: dict[str, dict[str, Any]] = {}
        self._remote_project_pickers: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._remote_tab_closers: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._remote_question_future: asyncio.Future | None = None
        # Live tool output arrives on the shell's background loop thread, one
        # ~1KB chunk at a time. It is buffered here and painted in batches.
        self._tool_out_lock = threading.Lock()
        self._tool_out_pending: dict[str, OutputTail] = {}
        self._tool_out_scheduled = False
        # Voice I/O (lazy: only built when first used; None when deps absent).
        self._voice_pipeline: Any = None
        self._voice_listening: bool = False
        self._voice_capturing: bool = False
        self._voice_speak_responses: bool = False  # set from config on first use
        # Tool/subagent names used during the current remote turn — collapsed
        # into the compact live status line's condensed counts.
        self._remote_activity: list[str] = []
        # The per-turn live status line (edits one compact message in place).
        self._remote_status: Any = None
        self._remote_answer: Any = None
        self._local_remote_streams: list[Any] = []
        self._remote_consumer_worker: Any = None
        # The MatrixRain animation widget, tracked so it can be removed on clear.
        self._home_banner: Static | None = None
        # name → async (args) -> str  for slash commands contributed by plugins.
        self._plugin_commands: dict[str, Any] = {}
        # Strong refs to fire-and-forget background sends so the event loop
        # doesn't garbage-collect them mid-flight (asyncio only holds weak refs).
        self._bg_tasks: set[Any] = set()
        # Rendering and state swaps share a lock: an awaited mount must finish
        # in its own pane before another switch replaces the widget references.
        self._pane_render_lock = asyncio.Lock()
        self._pane_replay_workers: dict[str, Any] = {}
        self._replaying_history = False
        self._tab_mounts: dict[str, Any] = {}
        self._tab_refresh_timer = None
        self._tab_refresh_period = None
        self._tab_render_signature = None
        self._tab_switch_generation = 0
        self._tab_switch_pending = 0
        # One-shot guard for the Ollama CPU-offload advisory (set once the model
        # is loaded and checked, so we don't spawn `ollama ps` every turn).
        self._ollama_offload_checked = False
        self._btw_agent: Any = None  # lazy-init btw side-channel agent (web-search only)
        self._bg_job_count: int = 0  # monotonic counter for background shell jobs
        self._bg_agent_tasks: dict[str, Any] = {}
        # Notes for detached (Ctrl+B) shell jobs that finished; prepended to the
        # agent's next turn so it learns of completions without an interrupt.
        self._pending_job_notes: list[str] = []
        # 1s timer that ticks the ⚙ tasks-bar runtime; only runs while tasks are
        # active (see _refresh_tasks_bar) so it never interferes when idle.
        self._tasks_timer: Any = None
        # Todo data for the docked checklist (#todo-dock). Per-pane, because
        # the dock widget is app-global: a session switch repaints from these.
        self._todos: list = []
        self._todos_agent: str | None = None
        self._todos_collapsed: bool = False
        # _init_widget and _init_steps removed — /init progress is now _log-only
        # Live per-iteration Ralph cards, keyed by iteration number, so an
        # IterationFinished event can update the card mounted at its start.
        self._ralph_iter_cards: dict[int, Static] = {}
        self._skill_names_cache: list[str] | None = None
        # Enabled-skill count shown in the status bar (total minus curation-
        # disabled), cached briefly; invalidated to None on a /skills toggle.
        self._skill_count_cache: int | None = None
        self._skill_count_ts: float = 0.0
        self._agent_names_cache: list[str] | None = None
        # Responsive layout flags, driven by _apply_responsive_layout on resize.
        self._narrow = False
        self._compact = False
        self._short = False
        self._tiny = False
        self._root_screen = None
        # Live status state (animated spinner + elapsed while a turn runs).
        self._activity = "ready"
        self._turn_active = False
        # The in-flight remote-bridge turn, if any. Held so escape can cancel
        # that turn without cancelling the consumer loop that received it.
        self._remote_turn_task: asyncio.Task | None = None
        # Set while a Ctrl+B detach cancels the turn, so the CancelledError path
        # shows a "moved to background" note instead of "Cancelled."
        self._detach_cancelling = False
        self._turn_start = 0.0
        self._spinner_frame = 0
        # Input mode pulse animation (plan / bash) — see _set_input_pulse.
        self._input_pulse_mode: str | None = None
        self._input_pulse_timer: Any = None
        self._pulse_on = False
        # Per-keystroke de-churn: last (plan, bash) mode state + last palette list.
        self._last_mode_state: tuple[bool, bool, bool] | None = None
        self._last_palette: list[str] | None = None
        # Notification badge: last seen unread count (drives status refresh).
        self._last_notif_count = 0
        self._desktop_notifier = None
        # Context-window management: warn once per crossing; auto-compact at critical.
        self._ctx_warned = False
        self._auto_compact = True
        # Live steering: SteeringInstructions added mid-turn, removed when it ends.
        self._live_steers: list = []
        # Deferred prompts: messages sent during an active turn that weren't
        # consumed by the steering middleware are re-dispatched as new turns.
        self._deferred_prompts: list[str] = []
        # Slash/bash commands submitted during an active turn: queued to RUN as
        # commands once the turn ends (not steered / not sent to the agent as text).
        self._deferred_commands: list[str] = []
        # Nova learning status (review cycles) shown inline in the #status line
        # beside the context %, so it never overlaps the input. _nova_status is
        # the current message (or None); the timer auto-clears it after a moment.
        self._nova_status: str | None = None
        self._nova_status_style: str = "dim"
        self._nova_indicator_timer: Any = None
        self._compaction_indicator_timer: Any = None
        self._compaction_indicator_frame = 0
        self._os_focused = True
        self._status_timer: Any = None
        # Dynamic-subagents panel. Rows are upserted by task id (a dispatch is
        # emitted twice, at start and at completion), and the phase order is the
        # order phases first appeared, which is the order they were watched.
        self._subagent_rows: dict[str, Any] = {}
        self._subagent_phase_order: list[str] = []
        self._subagent_collapsed_phases: set[str] = set()
        self._subagents_collapsed = False
        #: Set when a turn ends: the rows stay readable (collapsed) until the
        #: next turn's first dispatch replaces them.
        self._subagents_stale = False
        self._subagents_rows_by_line: list[tuple[str | None, str | None]] = []
        #: Cell where the task table starts; a click left of it is on a phase.
        self._subagents_task_column = 0
        #: What each row has done so far (task id -> preview entries), shown
        #: when the row is clicked.
        self._subagent_previews: dict[str, list[tuple[str, str]]] = {}
        self._subagents_paint_timer: Any = None
        #: Monotonic deadline for the coalesced repaint. The coalescer is gated on
        #: this rather than on the timer handle, because the handle is cleared only
        #: by the callback: a one-shot timer dropped by Textual (its callback is a
        #: ``call_next`` job, so it is lost if the app stops processing messages)
        #: would otherwise stay set and block every later repaint for the rest of
        #: the session. A deadline simply expires.
        self._subagents_paint_due: float = 0.0
        self._subagents_tick: Any = None

    def _current_agent_info(self) -> tuple[str, str | None]:
        """The speaking assistant's display name and its identity colour.

        The main agent returns ``None`` rather than a hardcoded green: it has no
        identity colour of its own, and pinning one meant its label never followed
        ``/theme``. A named subagent still returns the colour registered from its
        ``agent.md``, which is what keeps it distinct from the main agent.
        """
        from novacode_cli.core.input_preparation import get_agent_display_name
        from novacode_cli.config.config import MAIN_AGENT_ID, get_agent_color

        aid = self._current_assistant_id or self.assistant_id
        name = get_agent_display_name(aid)
        if not aid or aid == MAIN_AGENT_ID:
            return name, None
        return name, get_agent_color(aid)

    def compose(self) -> ComposeResult:
        # One scroll region per session, swapped by a ContentSwitcher. The root
        # pane deliberately keeps id="transcript" so existing CSS, the widget
        # cache and every test that queries "#transcript" are unaffected while
        # there is only one session.
        # Hidden until a second session exists, so a single-session run looks
        # exactly as it did before.
        yield Tabs(id="session-tabs")
        # Dynamic subagents: every dispatch of the turn so far, grouped into the
        # phases they were launched in. Hidden until something is in flight.
        yield SubagentsDock(
            Static("", id="subagents-title"),
            Static("", id="subagents-body"),
            id="subagents-dock",
        )
        with Horizontal(id="workspace-layout"):
            with ContentSwitcher(initial="transcript", id="panes"):
                yield TranscriptScroll(id="transcript")
            yield HarnessDock(id="ui-harness-dock")
        yield OptionList(id="cmdpalette")
        with Vertical(id="prompt-dock"):
            # Todos live INSIDE the prompt dock, not as a second
            # dock:bottom sibling: two bottom-docked siblings both claim the
            # same rows, so the checklist rendered on top of the input and
            # only appeared after a resize forced a reflow.
            with VerticalScroll(id="todo-scroll"):
                yield Static("", id="todo-dock")
            # "Jump to latest" sits at the top-right of the footer, directly above
            # the status/skills bar. The outer row is a transparent, right-aligning
            # strip; the inner Horizontal shrinks to the text (width: auto) and the
            # Static inside it is the only click target — the empty space to the
            # left of the words is not part of the button.
            with (
                Horizontal(id="jump-latest-row"),
                Horizontal(id="jump-latest-box"),
            ):
                yield Button(
                    "↓ Jump to latest",
                    id="jump-latest",
                    tooltip="Return to the newest message (Ctrl+End)",
                )
            with Horizontal(id="status-row"):
                # A `1fr` left cell + an `auto` right cell is what actually
                # right-aligns the counts. Appending them to the status Text
                # could only ever produce a ragged trailing gap, because a Rich
                # Text cannot right-align itself.
                yield Static("", id="prompt-hint-bar")
                yield Static("", id="status-counts")
            # The ask_user_question answer list. Docked here rather than pushed
            # as a modal so the transcript stays visible and the footer keeps
            # one layout; hidden until a question arrives. Sits directly above
            # the input, which is where the eye already is.
            yield QuestionDock(id="question-dock")
            with Horizontal(id="prompt-row"):
                yield Static("> ", id="prompt-prefix")
                yield PromptInput(
                    placeholder="Type your message or @path/to/file  (shift+enter for a new line)",
                    id="prompt",
                    paste_tracker=self.paste_tracker,
                    on_large_paste=self._on_large_paste,
                    on_clipboard_image=self._on_clipboard_image,
                )
            # Input-mode badge (normal / bash / plan / goal). Empty and 1 row
            # tall in normal mode, so it costs nothing until a mode is active.
            yield Static("", id="mode-badge")
            # Persistent background-tasks indicator (hidden until a task runs).
            # Click it (or Ctrl+B with nothing running) to open the tasks panel.
            yield Static("", id="tasks-bar")
            with Horizontal(id="info-bar"):
                with Vertical(id="col-workspace", classes="info-col"):
                    # The single accent tick marks where the field row starts;
                    # the rest of the labels stay dim so the values carry the eye.
                    yield Static("◆ WORKSPACE", classes="info-label first")
                    yield Static("", id="info-workspace", classes="info-value")
                with Vertical(id="col-branch", classes="info-col"):
                    yield Static("BRANCH", classes="info-label")
                    yield Static("", id="info-branch", classes="info-value")
                with Vertical(id="col-sandbox", classes="info-col"):
                    yield Static("SANDBOX", classes="info-label")
                    yield Static("", id="info-sandbox", classes="info-value")
                with Vertical(id="col-model", classes="info-col"):
                    yield Static("MODEL", id="info-model-label", classes="info-label")
                    yield Static("", id="info-model", classes="info-value")
                with Vertical(id="col-session", classes="info-col"):
                    yield Static("SESSION", classes="info-label")
                    yield Static("", id="info-quota", classes="info-value")
                # Persistent artifacts component — fixed in the footer, click (or
                # /artifacts) to open the list. Updates live via a registry observer.
                with Vertical(id="col-artifacts", classes="info-col"):
                    yield Static("◆ ARTIFACTS", classes="info-label")
                    yield Static("", id="info-artifacts", classes="info-value")

    @property
    def _palette(self) -> FooterPalette:
        """The footer palette for THIS app's active theme.

        Preferred over the module-level :func:`_active_palette` everywhere an
        instance is available: it reads the theme off ``self``, so it works on
        worker threads too (``active_app`` is a ContextVar that worker threads
        cannot see, which raised LookupError from ``_refresh_branch_worker``).
        """
        from novacode_cli.tui.palette import cached_palette

        try:
            return cached_palette(self.get_theme(self.theme))
        except Exception:  # noqa: BLE001
            return _active_palette()

    def _apply_saved_theme(self) -> None:
        """Register Nova's palettes and apply the persisted theme (or default)."""
        from novacode_cli.tui.themes import NOVA_EXTRA_THEMES

        for theme in (NOVA_TOKYO_NIGHT, NOVA_MATRIX, *NOVA_EXTRA_THEMES):
            try:
                self.register_theme(theme)
            except Exception:  # noqa: BLE001
                pass
        name = DEFAULT_THEME
        try:
            from novacode_cli.config.nova_config import NovaConfig

            saved = NovaConfig().get("theme")
            if saved and saved in self.available_themes:
                name = saved
        except Exception:  # noqa: BLE001
            pass
        try:
            self.theme = name
        except Exception:  # noqa: BLE001
            pass

    def _watch_theme(self, theme_name: str) -> None:
        """Recolour the footer's Rich Text when the theme changes.

        The status line, info bar and prompt row carry colours baked into Rich
        ``Text`` objects, which CSS never touches — so ``/theme`` recoloured the
        CSS-driven widgets while the footer kept its old hexes. Rebuild them
        here, after the base class has applied the new theme.
        """
        super()._watch_theme(theme_name)  # type: ignore[misc]
        # The palette is keyed by theme name; clearing the tail forces every
        # segment to be rebuilt with the new colours on the next refresh.
        self._status_tail = None
        self._status_right = None
        with suppress(Exception):  # a repaint must never break a theme switch
            self._refresh_status()
            self._refresh_info_bar()
            self._restyle_transcript_labels()
        self._apply_markdown_theme()

    def _restyle_transcript_labels(self) -> None:
        """Re-paint every mounted role header in the new theme's colours.

        A role header's colour lives in a Rich ``Text`` style, which CSS never
        touches — so without this, ``/theme`` only ever affected messages added
        afterwards and the transcript already on screen kept the old theme's
        hexes.

        The name and the identity colour are read off the message's own ``_header``
        (the source of truth ``update_header`` already maintains), never off the
        mounted ``Static``'s private content, which is not a reliable handle on
        this Textual version. A subagent's identity colour is carried through
        unchanged — those are deliberately fixed per assistant, not theme-derived.
        """
        for msg in self.query(ChatMessage):
            try:
                header = msg._header
                if not isinstance(header, Text):
                    continue
                # Collapse carets are appended to the header text; drop the suffix.
                name = header.plain.rstrip().removesuffix("▸").removesuffix("▾").rstrip()
                if msg.has_class("user"):
                    restyled = self._user_label(name or "You")
                else:
                    # _custom_color is the identity colour parsed off the old
                    # header; feeding it back keeps ralph pink and Nova themed.
                    restyled = self._agent_label(name or "Nova", msg._custom_color)
                msg.update_header(restyled)
            except Exception:  # noqa: BLE001 — one bad header must not break /theme
                continue

    def _apply_markdown_theme(self) -> None:
        """Point Rich's markdown elements at the active theme's colours.

        A reply's headings and code arrive through ``rich.markdown.Markdown``,
        which resolves ``markdown.h2`` / ``markdown.code`` against the console's
        theme. Those defaults are ANSI (``underline magenta``), so headings were
        whatever the terminal paints for magenta and no ``/theme`` ever moved
        them.

        Called from the theme watcher, which Textual fires at startup too — even
        for an assignment that does not change the value, which is what makes one
        hook enough. Both consoles are armed: the app's renders the transcript's
        Rich renderables, and the global one backs ``_capture`` for the tool
        panels and the interrupts that print markdown.
        """
        from novacode_cli.tui.palette import apply_markdown_theme

        try:
            theme = self.get_theme(self.theme)
        except Exception:  # noqa: BLE001
            return
        if theme is None:
            return
        for console in (self.console, _rich_console):
            with suppress(Exception):  # a colour must never break a repaint
                apply_markdown_theme(console, theme)

    async def on_mount(self) -> None:
        import threading

        self._load_ui_preferences()
        self._thread_id = threading.get_ident()
        # Log what the loop was running whenever the UI freezes for >1s
        # (~/.nova/logs/freeze.log). The UI and the agent share this loop, so
        # the stack is the culprit, wherever it lives.
        try:
            from novacode_cli.tui.stall_watch import StallWatch

            self._stall_watch = StallWatch(asyncio.get_running_loop())
            self._stall_watch.start()
            # A GC pause is a UI freeze on this shared loop; see tui/gc_tuning.py.
            from novacode_cli.tui import gc_tuning

            gc_tuning.tune()
        except Exception:  # noqa: BLE001 — diagnostics must never stop the app
            self._stall_watch = None
        # Build the slash-autocomplete skill list off the loop now: built lazily
        # on the first "/" keystroke it scans ~750 SKILL.md files on the loop.
        threading.Thread(target=self._get_skill_names, name="nova-skill-names", daemon=True).start()
        # Register Nova's palette and apply the saved (or default) theme first,
        # so the whole UI renders with the right colors from the first frame.
        self._apply_saved_theme()
        self.set_interval(5, self._schedule_session_autosave)
        from novacode_cli.config.config import settings

        self._ui_harness = UIHarness(self, settings.get_workspace_root() or Path.cwd())
        harness_notice = await self._ui_harness.initialize()
        if harness_notice:
            self._log(Text(harness_notice, style="yellow"))
        # Warm the singleton-widget cache so hot paths (streaming, status ticks,
        # keystrokes) skip the query_one DOM walk. See _w().
        for _sel, _kind in (
            ("#transcript", VerticalScroll),
            ("#prompt", PromptInput),
            ("#mode-badge", Static),
            ("#cmdpalette", OptionList),
            ("#prompt-hint-bar", Static),
            ("#status-counts", Static),
            ("#jump-latest-row", Horizontal),
            ("#jump-latest-box", Horizontal),
            ("#jump-latest", Button),
            ("#info-workspace", Static),
            ("#info-branch", Static),
            ("#info-sandbox", Static),
            ("#info-model-label", Static),
            ("#info-model", Static),
            ("#info-quota", Static),
        ):
            try:
                self._w_cache[_sel] = self.query_one(_sel, _kind)
            except NoMatches:
                pass
        self.query_one("#cmdpalette", OptionList).display = False
        self._root_screen = self.screen
        self._init_root_pane()
        self._bind_notification_source(self._root_pane)
        import sys

        if sys.platform == "win32" and not self._driver.is_headless:
            from novacode_cli.desktop_notifications import DesktopNotifier

            try:
                self._desktop_notifier = DesktopNotifier(
                    lambda sid, nid: self.post_message(DesktopNotificationClicked(sid, nid))
                )
                self._desktop_notifier.start()
            except Exception:
                logger.warning("Could not start desktop notifications", exc_info=True)
        self._set_status("ready")
        self._update_mode_badge()
        self._refresh_hint_bar()
        self._refresh_info_bar()
        self.run_worker(self._check_nova_update(), name="nova-update-check", group="nova-updates")
        self.set_interval(3600, self._schedule_nova_update_check)
        # Background tasks (Ctrl+B): observe the registry so the persistent
        # indicator + notifications update reactively; tick once a second so the
        # runtime clock advances while tasks run.
        try:
            from novacode_cli.shell.jobs import get_registry

            self._task_registry = get_registry()
            self._task_registry.add_observer(self._on_task_event_threadsafe)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_tasks_bar()
        # Persistent artifacts component: observe the registry + show initial count.
        # bind_session first, so a resumed session's artifacts are back in the
        # registry (and its count) before the component renders.
        try:
            from novacode_cli.artifacts.registry import bind_session as _bind_artifacts
            from novacode_cli.artifacts.registry import get_registry as _get_art_registry

            _restored_artifacts = _bind_artifacts(
                getattr(self.session_state, "session_id", "") or "",
                getattr(self.session_manager, "sessions_dir", None),
            )
            self._artifact_registry = _get_art_registry()
            self._artifact_registry.add_observer(self._on_artifact_event_threadsafe)
            if _restored_artifacts:
                self._log(
                    Text(
                        f"◈ restored {_restored_artifacts} artifact(s) from this session",
                        style="dim",
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        self._refresh_artifacts_component()
        # Keep the info bar live: model / branch / sandbox / quota can change
        # outside any single command (e.g. the agent runs `git checkout`), so
        # refresh on a slow timer (branch git runs off-thread, so it's cheap).
        self.set_interval(3.0, self._refresh_info_bar)
        # Load slash commands contributed by enabled plugins (TUI dispatch).
        self._load_plugin_commands()
        # Poll slowly while idle; animate focused active turns at 10 Hz.
        self._schedule_status_tick()
        self.query_one("#prompt", PromptInput).focus()
        # Show ASCII art banner on home screen
        self._show_home_banner()
        # Initialize explicitly enabled voice; native models remain lazy.
        await self._eager_voice_warmup()
        # Replay prior conversation when resuming a session.
        self._replay_history()
        # ...and measure what that history costs, so a continued session opens
        # with a real ctx% instead of the empty-window baseline. No-ops when
        # there is no history. on_mount is sync, so this runs as a worker.
        self.run_worker(self._update_context_breakdown(), exclusive=False)
        # Route remote bridge status messages into the transcript (not stdout).
        mgr = getattr(self.session_state, "_remote_bridge_manager", None)
        if mgr is not None:

            async def _status_cb(m: str) -> None:
                self._log(Text(f"🔗 Remote: {m}", style="dim"))

            try:
                mgr.set_status_callback(_status_cb)
                self._remote_status_callback = _status_cb
            except Exception:  # noqa: BLE001
                pass
        # Consume remote (Discord/Telegram) messages and render them in the TUI.
        if getattr(self.session_state, "_remote_message_queue", None) is not None:
            self._ensure_remote_consumer()
        # Register tool output callback for live terminal/command execution streaming
        try:
            from novacode_cli.events import register_tool_output_callback

            register_tool_output_callback(self._on_tool_output)
        except Exception:
            pass

    def _post_to_ui(self, callback: Any, *args: Any) -> None:
        """Run ``callback(*args)`` on the UI thread WITHOUT waiting for it.

        Textual's ``call_from_thread`` blocks the calling thread until the UI has
        run the callback. The callers here are the shell's shared background
        loop and tool threads: blocking them on the UI paced every running
        command and background task to the UI's speed, and stalled them all
        whenever the UI was busy. Order is preserved (FIFO scheduling). During
        shutdown the update is dropped rather than run off the UI thread.
        """
        if getattr(self, "_thread_id", None) == threading.get_ident():
            callback(*args)
            return
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return

        async def run() -> None:
            try:
                with self._context():
                    callback(*args)
            except Exception:  # noqa: BLE001 — a UI update must never kill the caller
                logger.debug("UI update failed", exc_info=True)

        update = run()
        try:
            asyncio.run_coroutine_threadsafe(update, loop)
        except RuntimeError:  # loop closed between the check and the call
            update.close()

    def _on_tool_output(self, call_id: str, text: str) -> None:
        """Queue a chunk of live tool output; it is painted in batches (<=20/s)."""
        with self._tool_out_lock:
            if getattr(self, "_output_stopped", False):
                return
            buffer = self._tool_out_pending.get(call_id)
            if buffer is None:
                if len(self._tool_out_pending) >= MAX_PENDING_CALLS:
                    # Only the live display is trimmed; final tool results
                    # still arrive through the normal event/session path.
                    self._tool_out_pending.pop(next(iter(self._tool_out_pending)))
                buffer = self._tool_out_pending[call_id] = OutputTail()
            buffer.append(text)
            if self._tool_out_scheduled:
                return
            self._tool_out_scheduled = True
        self._post_to_ui(self.set_timer, 0.05, self._flush_tool_output)

    def _flush_tool_output(self) -> None:
        with self._tool_out_lock:
            pending, self._tool_out_pending = self._tool_out_pending, {}
            self._tool_out_scheduled = False
        for call_id, buffer in pending.items():
            self._write_tool_output(call_id, _display_tail(buffer.drain()))

    def _write_tool_output(self, call_id: str, text: str) -> None:
        """Append live output to the widget showing ``call_id`` (UI thread)."""
        if call_id in self._tool_components:
            comp, body, base = self._tool_components[call_id]
            if isinstance(body, OutputLog):
                body.write(Text(text))
        elif call_id in self._subagent_tool_to_task:
            subagent_cid = self._subagent_tool_to_task[call_id]
            if subagent_cid in self._subagent_widgets:
                comp, body, stype, start_time = self._subagent_widgets[subagent_cid]
                try:
                    log_widget = body.query_one("#subagent-log", OutputLog)
                    if not log_widget.has_class("active"):
                        log_widget.add_class("active")
                    log_widget.write(Text(text))
                    comp._log_lines = getattr(comp, "_log_lines", 0) + text.count("\n")
                    log_widget.styles.height = min(max(comp._log_lines + 2, 5), 8)
                except Exception:
                    pass
        elif self._tool_group_body is not None:
            try:
                log_widget = self._tool_group_body.query_one("#tool-group-log", OutputLog)
                if log_widget.has_class("active"):
                    log_widget.write(Text(text))
                    self._tool_group_log_lines += text.count("\n")
                    log_widget.styles.height = min(max(self._tool_group_log_lines + 2, 5), 8)
            except Exception:
                pass

    # -- OS focus handlers ----------------------------------------------------
    # Pause/resume the MatrixRain animation when the terminal window gains or
    # loses OS-focus, so the TUI never spins the CPU on an invisible animation.

    def _matrix_rain(self) -> MatrixRain | None:
        """Return the MatrixRain widget if it is mounted, else None."""
        try:
            return self.query_one("#matrix-rain", MatrixRain)
        except NoMatches:
            return None

    def _watch_app_focus(self, focus: bool) -> None:
        """Textual's focus bookkeeping, minus its whole-screen restyle.

        The base watcher starts with ``self.screen.update_node_styles()``, which
        re-applies the stylesheet to every widget on screen so that rules keyed
        on ``App:focus`` / ``App:blur`` can take effect. Nova has no such rule,
        and with a long transcript that pass froze the UI for up to 5.4 s on
        every alt-tab (freeze.log). The focused widget still restyles itself
        through ``set_focus``.

        ponytail: shadows one method for the duration of the call instead of
        copying Textual's body; add an ``App:blur`` rule and this must go.
        """
        screen = self.screen
        screen.update_node_styles = lambda animate=True: None  # type: ignore[method-assign]
        try:
            super()._watch_app_focus(focus)
        finally:
            del screen.update_node_styles

    def on_app_blur(self) -> None:
        """Pause MatrixRain when the terminal loses OS focus."""
        self._os_focused = False
        self._schedule_status_tick()
        # The user just looked away: the one moment a full GC pass is invisible.
        from novacode_cli.tui import gc_tuning

        gc_tuning.collect_while_idle(busy=self._turn_active)
        rain = self._matrix_rain()
        if rain is not None:
            rain.pause()

    def on_app_focus(self) -> None:
        """Resume MatrixRain when the terminal regains OS focus."""
        self._os_focused = True
        self._schedule_status_tick()
        rain = self._matrix_rain()
        if rain is not None:
            vp = getattr(self, "_voice_pipeline", None)
            if vp is None or not vp.tts_active:
                rain.resume()

    # -- helpers --------------------------------------------------------------
    def _w(self, selector: str, kind: Any) -> Any:
        """Return a cached singleton widget, resolving (and caching) on first use.

        Avoids a `query_one` DOM walk on every delta / keystroke / status tick.
        Raises NoMatches (like query_one) if the widget isn't mounted yet — hot
        callers that may run before mount guard with try/except.
        """
        w = self._w_cache.get(selector)
        if w is None:
            try:
                w = self.query_one(selector, kind)
            except NoMatches:
                if self.screen_stack:
                    w = self.screen_stack[0].query_one(selector, kind)
                else:
                    raise
            self._w_cache[selector] = w
        return w

    def _transcript(self) -> VerticalScroll:
        """The active session's scroll region.

        Resolved through the active pane rather than the ``_w`` cache: that cache
        keys on the selector, so after a tab switch it would keep handing back
        the previous pane's widget and content would land in the wrong session.
        """
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.scroll is not None:
            return pane.scroll
        return self._w("#transcript", VerticalScroll)

    # ── session panes ────────────────────────────────────────────────────

    def _init_root_pane(self) -> None:
        """Register the in-process session as pane 0.

        Called once at mount. With a single session this changes nothing the user
        can see: the pane simply wraps the existing ``#transcript`` widget.
        """
        from novacode_cli.tui.session_pane import SessionPane

        try:
            scroll = self.query_one("#transcript", VerticalScroll)
        except NoMatches:  # pragma: no cover - compose always yields it
            return
        pane = SessionPane(
            sid="root",
            title=((getattr(self.session_state, "session_id", "") or "main")[:8]),
            scroll=scroll,
            kind="root",
        )
        self._panes: list = [pane]
        self._root_pane = pane
        self._active_pane = pane
        # Seed the (hidden) tab bar now so it is never empty when a session is
        # spawned later. See _refresh_tabs for why an empty bar is a problem.
        self._refresh_tabs()

    def _pane_for(self, sid: str):
        """The pane owning session *sid*, or None."""
        for pane in getattr(self, "_panes", []):
            if pane.sid == sid:
                return pane
        return None

    async def _deliver(self, pane, event) -> None:
        """Route one stream event to *pane*: render if visible, else buffer.

        A hidden pane must not draw, because the app's widget references
        (``_stream_msg``, ``_tool_group``, …) belong to whichever pane is
        currently swapped in — rendering into a hidden pane would scribble into
        the visible one. Transient deltas are dropped rather than buffered: the
        authoritative text arrives as ``AssistantMessage``, so replaying a
        thousand keystroke-sized fragments on switch would be pure cost.
        """
        self._feed_remote(pane, event)
        if pane is None:  # before panes exist (early mount) — render directly
            await self._render(event)
            return
        pane.status = _status_for_event(event, pane.status)
        self._sync_child_activity(pane)
        async with self._pane_render_lock:
            if pane is getattr(self, "_active_pane", pane):
                if pane.buffer:
                    # Preserve order while history catches up. The pipe reader
                    # need not wait for the entire transcript to be mounted.
                    self._queue_pane_event(pane, event)
                    self._ensure_pane_replay(pane)
                else:
                    await self._render(event)
            elif not isinstance(event, (ev.TextDelta, ev.ReasoningDelta, ev.StatusUpdate)):
                pane.buffer.append(event)
                pane.unread += 1

    @staticmethod
    def _queue_pane_event(pane, event) -> None:
        """Coalesce queued live previews so tokens cannot evict real history."""
        if pane.buffer and isinstance(event, (ev.TextDelta, ev.ReasoningDelta, ev.StatusUpdate)):
            previous = pane.buffer[-1]
            if type(previous) is type(event):
                if isinstance(event, ev.StatusUpdate):
                    pane.buffer[-1] = event
                else:
                    limit = _LIVE_PREVIEW_CHARS if isinstance(event, ev.TextDelta) else 2000
                    pane.buffer[-1] = type(event)(
                        text=(previous.text[-limit:] + event.text[-limit:])[-limit:]
                    )
                return
        pane.buffer.append(event)

    async def _switch_to(self, pane) -> None:
        """Make *pane* the visible session, swapping conversation state with it."""
        self._tab_switch_generation += 1
        generation = self._tab_switch_generation
        self._refresh_tabs()
        # add_tab() finishes asynchronously. Its first-tab auto-activation
        # must finish before we select the requested session.
        mounts = [mount for mount in self._tab_mounts.values() if not mount.is_done]
        if mounts:
            self._tab_switch_pending += 1
            try:
                await asyncio.gather(*mounts)
            finally:
                self._tab_switch_pending -= 1
        async with self._pane_render_lock:
            if generation == self._tab_switch_generation and pane in self._panes:
                self._select_pane(pane)

    def _select_pane(self, pane) -> None:
        """Switch the viewport immediately; replay history in a separate worker."""
        current = getattr(self, "_active_pane", None)
        if pane is current:
            return
        if current is not None:
            current.save_from(self)
        if pane.has_state:
            pane.load_into(self)

        self._active_pane = pane
        self._refresh_terminal_title()
        try:
            self.query_one("#panes", ContentSwitcher).current = pane.scroll.id
        except NoMatches:  # pragma: no cover - switcher always present
            pass
        # Keep the tab highlight in sync with the pane that is actually active.
        # Re-entry is harmless: the TabActivated handler no-ops when the pane is
        # already active.
        with contextlib.suppress(Exception):
            tabs = self.query_one("#session-tabs", Tabs)
            if tabs.active != pane.sid:
                tabs.active = pane.sid

        # History can contain thousands of mounts. It must never hold the tab
        # switch handler open or interleave state swaps with an awaited mount.
        self._ensure_pane_replay(pane)
        pane.unread = 0

        # The todo dock is app-global but its data is per-pane, so repaint
        # it for the pane now on screen — otherwise the previous session's
        # checklist sits under this one.
        self._paint_todos(getattr(self, "_todos", None), getattr(self, "_todos_agent", None))
        self._refresh_status()
        self._update_mode_badge()
        self._refresh_tasks_bar()
        self._schedule_status_tick()
        self._scroll_end(force=False)
        self._update_jump_latest(measure=True)
        self._refresh_tabs()
        if self._stream_msg is not None or self._reason_msg is not None:
            self._schedule_stream_flush()
        if self._tool_group_entries:
            self._schedule_tool_group_refresh(running=self._tool_group_running)
        if pane.kind == "child" and pane.pending_interrupt is not None:
            self._handle_child_interrupt(pane)

    def _ensure_pane_replay(self, pane) -> None:
        if not pane.buffer or pane.sid in self._pane_replay_workers:
            return
        self._pane_replay_workers[pane.sid] = self.run_worker(
            self._replay_pane_events(pane), name=f"history-{pane.sid}", group="session-history"
        )

    async def _replay_pane_events(self, pane) -> None:
        """Render a few history events per frame, allowing input between batches."""
        completed = False
        try:
            while pane.buffer and pane is self._active_pane:
                async with self._pane_render_lock:
                    if pane is not self._active_pane:
                        break
                    # Approval modals must paint while waiting for the user;
                    # never keep Textual's batch-update context open around one.
                    if isinstance(pane.buffer[0], ev.InterruptRequest):
                        await self._render(pane.buffer.popleft())
                        continue
                    deadline = time.monotonic() + 0.008
                    # Deferring paint/scroll together avoids a layout and an
                    # auto-scroll for every individual historical message.
                    with self.batch_update():
                        self._replaying_history = True
                        try:
                            for _ in range(4):
                                if not pane.buffer:
                                    break
                                if isinstance(pane.buffer[0], ev.InterruptRequest):
                                    break
                                await self._render(pane.buffer.popleft())
                                if time.monotonic() >= deadline:
                                    break
                        finally:
                            self._replaying_history = False
                    self._scroll_end(force=False)
                    self._sync_child_activity(pane)
                await asyncio.sleep(0.005)
            completed = True
        finally:
            self._pane_replay_workers.pop(pane.sid, None)
            # A switch may have returned to this pane while the old worker was
            # finishing. Leave no active backlog without a renderer.
            if completed and pane.buffer and pane is self._active_pane and self.is_running:
                self._ensure_pane_replay(pane)

    def _bind_notification_source(self, pane):
        state = self.session_state if pane.kind == "root" else pane.state.get("session_state")
        if state is not None:
            state._notification_callback = lambda notification: self._desktop_notification(
                pane.sid, notification
            )

    def _desktop_notification(self, sid, notification):
        notifier = self._desktop_notifier
        if notifier is not None:
            pane = self._pane_for(sid)
            title = f"NovaCode · {pane.title if pane else sid} · {notification.title}"
            notifier.show(sid, notification.id, title, notification.message)
            return not notifier.closed.is_set()
        return False

    async def on_desktop_notification_clicked(self, event: DesktopNotificationClicked):
        pane = self._pane_for(event.sid)
        if pane is None or pane.status in ("crashed", "exited"):
            self.notify("That session has ended.", title="NovaCode")
            return
        await self._switch_to(pane)
        state = self.session_state
        notification = (
            self._find_notification(state, event.notification_id)
            if hasattr(state, "notifications")
            else None
        )
        if notification is not None:
            self._log(Text(f"{notification.title}\n{notification.message}"))
        self.query_one("#prompt", PromptInput).focus()

    async def on_unmount(self):
        # Unmount also runs after failed/interrupted tasks and run_test exits,
        # where action_quit may never have been reached.
        self._release_output_observers()
        watchdog = getattr(self, "_stall_watch", None)
        if watchdog is not None:
            watchdog.stop()
        for pane in getattr(self, "_panes", []):
            state = (
                self.session_state if pane is self._active_pane else pane.state.get("session_state")
            )
            if state is not None:
                state._notification_callback = None
        supervisor = getattr(self, "_session_supervisor", None)
        if supervisor is not None:
            with contextlib.suppress(Exception):
                await supervisor.close_all(timeout=1.0)
        watcher = getattr(self, "_async_watcher", None)
        if watcher is not None:
            with contextlib.suppress(Exception):
                await watcher.aclose()
        notifier = self._desktop_notifier
        if notifier is not None:
            await asyncio.to_thread(notifier.close)

    def _release_output_observers(self) -> None:
        """Stop producers and release process-global references to this app."""
        owner = self._remote_owner_state()
        manager = getattr(owner, "_remote_bridge_manager", None)
        callback = getattr(self, "_remote_status_callback", None)
        if callback is not None and getattr(manager, "_on_status", None) is callback:
            with contextlib.suppress(Exception):
                manager.set_status_callback(None)
        self._remote_status_callback = None
        for registry_attr, callback in (
            ("_task_registry", self._on_task_event_threadsafe),
            ("_artifact_registry", self._on_artifact_event_threadsafe),
        ):
            registry = getattr(self, registry_attr, None)
            if registry is not None:
                with contextlib.suppress(Exception):
                    registry.remove_observer(callback)
        from novacode_cli.events import unregister_tool_output_callback

        unregister_tool_output_callback(self._on_tool_output)
        with self._tool_out_lock:
            self._output_stopped = True
            self._tool_out_pending.clear()
            self._tool_out_scheduled = False

    async def _dispatch_to_child(self, pane, text: str) -> None:
        """Handle input while a spawned session's tab is active.

        Only session-management commands are interpreted locally; everything else
        is forwarded to the child as a prompt.

        # ponytail: other slash commands operate on self.agent, which a child
        # pane doesn't own. Forward a command channel to the child only if it
        # turns out people want /model, /compact etc. per session.
        """
        stripped = text.strip()
        low = stripped.lower()

        if low in _EXIT_COMMANDS:
            await self.action_quit()
            return
        if low == "/close":
            await self._close_session(pane)
            return
        if low in ("/tasks", "/jobs"):
            self._open_tasks_panel()
            return
        if low in ("/tabs", "/session") or low.startswith(("/tabs ", "/session ")):
            await self._run_tabs_command(stripped.replace("/session", "/tabs", 1))
            return
        if stripped.startswith("/") or stripped.startswith("!"):
            self._log(
                Text(
                    f"“{stripped.split()[0]}” isn't available inside a spawned session. "
                    "Use /close, or switch to the main session (alt+1).",
                    style="#e0af68",
                )
            )
            return

        await self._add_message(self._user_label(), "user", Markdown(stripped))
        if await self._send_child_prompt(pane, stripped) is None:
            self._log(Text("✖ that session is no longer running.", style="bold #f7768e"))
            return
        pane.status = "running"
        self._sync_child_activity(pane)
        self._refresh_tabs()

    async def _run_tabs_command(self, text: str) -> None:
        """``/tabs launch|new|list|close`` for parallel session tabs."""
        parts = text.split(maxsplit=2)
        sub = (parts[1] if len(parts) > 1 else "list").lower()
        rest = parts[2] if len(parts) > 2 else ""

        if sub in ("launch", "pick"):
            await self._launch_project_session(rest)
            return

        if sub == "new":
            name, _, task = rest.partition(":")
            await self.spawn_session(name.strip() or "session", task.strip())
            return

        if sub == "close":
            children = [
                pane
                for pane in self._panes
                if pane.kind == "child" and pane.status not in ("crashed", "exited")
            ]
            if rest.strip():
                pane = self._pane_for(rest.strip())
                if pane is not None and pane.kind == "child":
                    await self._close_session(pane)
                    return
            if not children:
                self._log(Text("There are no running session tabs to close.", style="#e0af68"))
                return
            labels = [f"{pane.title}  [{pane.status}]  ·  {pane.sid}" for pane in children]
            selected = await self.push_screen_wait(
                PickScreen("Close session tab", labels, "Select a running tab to close.")
            )
            if selected is not None and 0 <= selected < len(children):
                await self._close_session(children[selected])
            return

        block = Text()
        for i, pane in enumerate(getattr(self, "_panes", []), 1):
            glyph = self._PANE_GLYPHS.get(pane.status, "●")
            marker = "→" if pane is self._active_pane else " "
            block.append(f"{marker} {i}. {glyph} {pane.title}  [{pane.status}]")
            if pane.branch:
                block.append(f"  {pane.branch}", style="dim")
            block.append("\n")
        block.append(
            "\n/tabs new <name>[: task] · /tabs launch · /tabs close · alt+<n>",
            style="dim",
        )
        self._log(block)

    async def _launch_project_session(
        self, arguments: str = "", *, folder: str | None = None, task: str | None = None
    ) -> None:
        """Pick an approved project, collect its task, and require launch approval."""
        from pathlib import Path
        from rich.text import Text

        from novacode_cli.sessions.launch import (
            approved_projects,
            prepare_launch,
            validate_approved_folder,
        )

        projects = await asyncio.to_thread(approved_projects)
        if not projects:
            self._log(
                Text("No existing Nova-approved project folders are available.", style="yellow")
            )
            return

        preferred = folder
        task_value = task or ""
        name = None
        if folder is None:
            task_match = re.search(
                r"(?:^|\s)--task(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|(.+?))(?=\s+--|$)",
                arguments,
            )
            name_match = re.search(
                r"(?:^|\s)--name(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|([^\s]+))",
                arguments,
            )
            if task is None and task_match:
                task_value = next((part for part in task_match.groups() if part is not None), "")
            if name_match:
                name = next((part for part in name_match.groups() if part is not None), None)
            preferred = (
                re.split(r"\s+--(?:name|task)\b", arguments, maxsplit=1)[0].strip().strip("\"'")
            )

        preferred_path = ""
        if preferred:
            with contextlib.suppress(OSError):
                preferred_path = str(
                    await asyncio.to_thread(Path(preferred).expanduser().resolve, strict=True)
                )
            if not preferred_path:
                basename_matches = [
                    path
                    for path in projects
                    if path.name.casefold() == preferred.strip('"').casefold()
                ]
                if len(basename_matches) == 1:
                    preferred_path = str(basename_matches[0])
        options = [(f"{path.name} — {path.parent}", str(path)) for path in projects]
        choice = await self.push_screen_wait(
            ProjectSessionPicker(
                options,
                task=task_value,
                preferred_folder=preferred_path or preferred,
            )
        )
        if not choice:
            return
        try:
            request = await asyncio.to_thread(
                prepare_launch, choice["folder"], name, choice["task"], str(Path.cwd())
            )
        except Exception as exc:
            self._log(Text(f"Could not prepare session launch: {exc}", style="bold #f7768e"))
            return
        try:
            await asyncio.to_thread(validate_approved_folder, request["folder"])
        except (OSError, ValueError):
            self._log(Text("The selected folder is no longer Nova-approved.", style="yellow"))
            return
        model = getattr(self, "model_name", "current model")
        telegram_metadata = self._launch_telegram_metadata()
        approval = (
            "auto-approve"
            if getattr(self.session_state, "auto_approve", False)
            else "approval required"
        )
        body = Text(
            f"Folder: {request['folder']}\nName: {request['name']}\n"
            f"Initial task: {request['task'] or '(none)'}\nModel/router: {model} / inherited profile\n"
            f"Tool approval: {approval}\nSandbox: destination default\n"
            f"Telegram: {telegram_metadata['chat_id'] if telegram_metadata else 'not configured'}\n"
            "Folder trust: approved\n\n"
            "Open this folder in a new session tab?",
        )
        if not await self.push_screen_wait(ConfirmModal("Approve new Nova session", body)):
            self._log(Text("Session launch cancelled.", style="dim"))
            return
        try:
            await asyncio.to_thread(validate_approved_folder, request["folder"])
            pane = await self.spawn_session(
                request["name"], request["task"], directory=Path(request["folder"])
            )
        except Exception as exc:
            self._log(Text(f"Session launch failed: {exc}", style="bold #f7768e"))
            return
        if pane is not None:
            self._log(Text(f"Opened session tab “{request['name']}”.", style="green"))

    # ── tab bar ──────────────────────────────────────────────────────────

    _PANE_GLYPHS = {
        "idle": "●",
        "running": "◐",
        "needs-approval": "⚠",
        "starting": "◌",
        "crashed": "✖",
        "exited": "○",
    }

    def _tab_remote_platforms(self, pane) -> list[str]:
        """Report live connections that can route messages to this pane."""
        if pane.status in ("crashed", "exited"):
            return []
        platforms = []
        router = getattr(self, "_remote_router", None)
        for bridge in self._remote_telegram_bridges():
            if getattr(bridge, "is_connected", False) and (
                pane.kind == "root"
                or (router is not None and router.topic_of(bridge._config.chat_id, pane.sid))
            ):
                platforms.append("TG")
                break
        if pane.kind == "root":
            from novacode_cli.remote.bridge import RemotePlatform

            manager = getattr(self._remote_owner_state(), "_remote_bridge_manager", None)
            if manager is not None and any(
                getattr(bridge, "is_connected", False)
                for bridge in manager.bridges(RemotePlatform.DISCORD)
            ):
                platforms.append("DC")
        return platforms

    def _refresh_tabs(self) -> None:
        """Redraw the session tab bar; hidden while there is only one session."""
        self._refresh_terminal_title()
        panes = getattr(self, "_panes", [])
        rows = []
        animated = False
        for i, pane in enumerate(panes, 1):
            status = pane.status
            # Root operations such as compaction and remote prompts can be busy
            # before their first stream event updates the pane status.
            if pane.kind == "root" and status == "idle":
                busy = (
                    self._turn_active
                    if pane is self._active_pane
                    else pane.state.get("_turn_active", False)
                )
                if busy:
                    status = "running"
            glyph = self._PANE_GLYPHS.get(status, "●")
            if status in ("running", "starting"):
                animated = True
                frames = "◐◓◑◒"
                glyph = frames[int(time.monotonic() * 4) % len(frames)]
            platforms = tuple(self._tab_remote_platforms(pane))
            tooltip = f"{pane.title} · {status}"
            if platforms:
                names = {"TG": "Telegram", "DC": "Discord"}
                tooltip += " · " + ", ".join(names[name] for name in platforms) + " connected"
            rows.append((pane.sid, i, glyph, pane.title, pane.unread, platforms, tooltip))

        # No periodic work is needed while the tab bar is hidden. With multiple
        # panes, idle state/remote indicators poll at 1 Hz; active spinners use
        # 4 Hz. Lifecycle handlers also refresh immediately on state changes.
        target_period = None if len(panes) <= 1 else (0.25 if animated else 1.0)
        if target_period != self._tab_refresh_period:
            if self._tab_refresh_timer is not None:
                self._tab_refresh_timer.stop()
                self._tab_refresh_timer = None
            self._tab_refresh_period = target_period
            if target_period is not None:
                self._tab_refresh_timer = self.set_interval(target_period, self._refresh_tabs)

        # Most timer ticks during an idle session change nothing. Avoid a DOM
        # query, widget traversal, Text construction, and label comparisons then.
        mount_state = tuple(sid for sid, mount in self._tab_mounts.items() if not mount.is_done)
        signature = (tuple(rows), mount_state, len(panes) > 1)
        if signature == self._tab_render_signature:
            return
        self._tab_render_signature = signature

        try:
            tabs = self.query_one("#session-tabs", Tabs)
            # Timers can fire while Textual dismantles the widget's children.
            tabs.query_one("#tabs-list")
        except NoMatches:  # pragma: no cover - compose always yields it
            self._tab_render_signature = None
            return

        # Keep the root tab mounted while hidden. That prevents the first child
        # from becoming the implicit selection when the bar becomes visible.
        tabs.display = len(panes) > 1

        # Update INCREMENTALLY; never clear() and rebuild. clear()+add_tab emits
        # a TabActivated for the first tab, delivered asynchronously — so it
        # landed after the rebuild and dragged the user back to pane 1. That is
        # why a freshly spawned session seemed to "do nothing": you were silently
        # returned to the root pane and everything you typed went to the root
        # agent. Adding a tab to a non-empty bar does not change the selection.
        existing = {t.id: t for t in tabs.query(Tab)}
        wanted = {sid for sid, *_ in rows}

        for sid in existing:
            if sid not in wanted:
                with contextlib.suppress(Exception):
                    tabs.remove_tab(sid)

        for sid, i, glyph, title, unread, platforms, tooltip in rows:
            unread_text = f" +{unread}" if unread else ""
            label = Text(f"{i}:{glyph} {title}{unread_text}")
            if platforms:
                label.append(f" [{' / '.join(platforms)}]", style="bold green")
            tab = existing.get(sid)
            if tab is None:
                mounting = self._tab_mounts.get(sid)
                if mounting is not None and not mounting.is_done:
                    continue
                tab = SessionTab(label, id=sid)
                tab.tooltip = tooltip
                self._tab_mounts[sid] = tabs.add_tab(tab)
            else:
                content = Content.from_text(label)
                if tab.label != content:
                    tab.label = content
                if tab.tooltip != tooltip:
                    tab.tooltip = tooltip
        self._tab_mounts = {
            sid: mount for sid, mount in self._tab_mounts.items() if not mount.is_done
        }

    def _refresh_terminal_title(self) -> None:
        """Show the active session's name in the terminal window or tab."""
        pane = getattr(self, "_active_pane", None)
        name = (
            getattr(pane, "title", None)
            or (getattr(self.session_state, "session_id", "") or "main")[:8]
        )
        # Session names are user input. OSC titles must contain no escape or
        # control characters that could terminate the title and inject output.
        name = " ".join(
            "".join(char for char in str(name) if char.isprintable() or char.isspace()).split()
        )[:120]
        title = f"NovaCode · {name or 'main'}"
        self.title = title
        driver = self._driver
        if driver is None or driver.is_headless or title == getattr(self, "_terminal_title", None):
            return
        try:
            # Write through Textual's driver so this never enters the transcript
            # or bypasses its serialized terminal output (including Windows).
            driver.write(f"\x1b]0;{title}\x07")
            driver.flush()
            self._terminal_title = title
        except Exception:  # noqa: BLE001 — title support must not break a session
            logger.debug("Could not update terminal title", exc_info=True)

    def _refresh_session_identity(self) -> None:
        """Keep the root tab and terminal title in step after resume or clear."""
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "root":
            pane.title = (getattr(self.session_state, "session_id", "") or "main")[:8]
        self._refresh_tabs()

    def on_tabs_tab_activated(self, event) -> None:
        """Clicking / keyboard-selecting a tab switches sessions.

        Ignored while the bar is being rebuilt: adding tabs emits TabActivated
        for the first one, which would otherwise drag the user back to pane 1
        every time a session is spawned or closed.
        """
        if getattr(self, "_rebuilding_tabs", False):
            return
        tab = getattr(event, "tab", None)
        tabs = getattr(event, "tabs", None)
        if self._tab_switch_pending or (
            tabs is not None and tabs.active != getattr(tab, "id", None)
        ):
            return
        pane = self._pane_for(getattr(tab, "id", "") or "")
        if pane is not None and pane is not getattr(self, "_active_pane", None):
            self.run_worker(self._switch_to(pane))

    # ── session actions ──────────────────────────────────────────────────

    def action_new_session(self) -> None:
        self.run_worker(self._prompt_new_session())

    def action_next_session(self) -> None:
        self._cycle_session(1)

    def action_prev_session(self) -> None:
        self._cycle_session(-1)

    def _cycle_session(self, delta: int) -> None:
        panes = getattr(self, "_panes", [])
        if len(panes) < 2:
            return
        try:
            idx = panes.index(self._active_pane)
        except ValueError:
            idx = 0
        self.run_worker(self._switch_to(panes[(idx + delta) % len(panes)]))

    def action_goto_session(self, number: int) -> None:
        """alt+<n>: jump straight to the nth session."""
        panes = getattr(self, "_panes", [])
        if 1 <= number <= len(panes):
            self.run_worker(self._switch_to(panes[number - 1]))

    # ── spawning child sessions ──────────────────────────────────────────

    def _supervisor(self):
        """The child-process manager, created on first use."""
        sup = getattr(self, "_session_supervisor", None)
        if sup is None:
            from novacode_cli.sessions.supervisor import SessionSupervisor

            sup = SessionSupervisor(self._on_child_message)
            self._session_supervisor = sup
        return sup

    async def _prompt_new_session(self) -> None:
        """Ctrl+N: ask for a name + task, then spawn."""
        # QuestionModal comes from the module-level re-export above: the
        # ask_user_question path was the only user of a local re-import, and
        # keeping it shadowed the re-export (ruff F811).
        answer = await self.push_screen_wait(
            QuestionModal(
                {
                    "question": (
                        "New parallel session — name it, optionally with a task:\n"
                        "  fix-parser: add retry logic to the HTTP client"
                    )
                }
            )
        )
        # QuestionModal dismisses with {"response": QuestionResponse}, and
        # QuestionResponse is a TypedDict — so the answer is response["answer"],
        # NOT an attribute. Reading it as an attribute silently stringified the
        # whole dict and named the session "{'answer'".
        raw = ""
        if isinstance(answer, dict):
            response = answer.get("response")
            if isinstance(response, dict):
                raw = str(response.get("answer") or "")
            elif isinstance(response, str):
                raw = response
        elif isinstance(answer, str):
            raw = answer
        if not raw.strip():
            return
        name, _, task = raw.partition(":")
        await self.spawn_session(name.strip() or "session", task.strip())

    async def spawn_session(
        self,
        name: str,
        task: str = "",
        *,
        directory: Path | None = None,
        auto_approve: bool | None = None,
    ) -> Any | None:
        """Create a child session in a worktree or approved folder and show its tab."""
        import uuid

        from types import SimpleNamespace
        from novacode_cli.sessions import worktree as wt
        from novacode_cli.tui.session_pane import SessionPane

        approval_mode = (
            bool(getattr(self.session_state, "auto_approve", False))
            if auto_approve is None
            else auto_approve
        )
        sup = self._supervisor()
        if sup.at_capacity():
            self._log(
                Text(
                    "⚠ Session limit reached — close one before starting another.",
                    style="bold #e0af68",
                )
            )
            return

        sid = f"s-{uuid.uuid4().hex[:8]}"
        if directory is None:
            self._log(Text(f"◌ preparing worktree for “{name}”…", style=self._palette.primary))
            try:
                start = Path.cwd()
                info = await asyncio.to_thread(
                    lambda: wt.create_worktree(name, repo=wt.repo_root(start), session_id=sid)
                )
            except Exception as e:  # noqa: BLE001 — surface, don't crash the TUI
                self._log(Text(f"✖ could not create worktree: {e}", style="bold #f7768e"))
                return None
        else:
            from novacode_cli.sessions.launch import validate_approved_folder

            try:
                target = await asyncio.to_thread(validate_approved_folder, directory)
            except (OSError, ValueError):
                self._log(Text("✖ session folder is not Nova-approved.", style="bold #f7768e"))
                return None
            info = SimpleNamespace(path=target, branch=None, warnings=[])

        for warn in info.warnings:
            self._log(Text(f"⚠ {warn}", style="#e0af68"))

        # Mount the pane immediately: agent build takes seconds and the UI must
        # never block on it.
        scroll = TranscriptScroll(id=f"pane-{sid}")
        await self.query_one("#panes", ContentSwitcher).mount(scroll)
        # ContentSwitcher only hides non-current children at COMPOSE time; one
        # mounted later stays visible and renders on top of the active pane. The
        # new (empty) pane then covered the running session's transcript, which
        # looked like everything had vanished. Hide it until switched to.
        scroll.display = False
        pane = SessionPane(
            sid=sid,
            title=name,
            scroll=scroll,
            kind="child",
            status="starting",
            worktree=info.path,
            branch=info.branch,
        )
        # A pane with no saved state inherits whatever is on the app when it is
        # switched to — including the previous pane's live widget refs. _render
        # reuses `_stream_msg` when it isn't None, so the child's reply streamed
        # into the ROOT pane's hidden widget and this tab stayed black. Start it
        # from a blank conversation instead.
        from novacode_cli.states.Session import SessionState
        from novacode_cli.tui.session_pane import fresh_state

        child_state = SessionState(auto_approve=approval_mode, no_splash=True)
        child_state.session_id = sid
        pane.state = fresh_state(
            session_state=child_state,
            assistant_id=self.assistant_id,
            model_name=self.model_name,
        )
        self._bind_notification_source(pane)
        self._panes.append(pane)
        self._refresh_tabs()
        self._pending_task_for = getattr(self, "_pending_task_for", {})
        if task:
            # Register before spawning: the worker can emit ``ready`` before
            # Telegram topic creation yields back to this coroutine.
            self._pending_task_for[sid] = task
        # Switch to it. Without this the new tab merely EXISTS while the root
        # session stays active, so everything typed next goes to the root agent
        # and renders in the root transcript — the spawned session just sits
        # there never receiving a prompt, which reads as "the tab does nothing".
        await self._switch_to(pane)

        try:
            child = await sup.spawn(
                session_id=sid,
                name=name,
                worktree=info.path,
                branch=info.branch,
                assistant_id=self.assistant_id or "nova-agent",
            )
        except Exception as e:  # noqa: BLE001
            getattr(self, "_pending_task_for", {}).pop(sid, None)
            pane.status = "crashed"
            self._refresh_tabs()
            self._log(Text(f"✖ could not start session: {e}", style="bold #f7768e"))
            return None

        pane.child = child
        await self._open_remote_topics(pane)
        note = Text()
        note.append(f"◆ you are now in session “{name}”\n", style="bold #9ece6a")
        note.append(f"   {info.path}", style="dim")
        if info.branch:
            note.append(f"  ·  {info.branch}", style="dim")
        note.append("\n   alt+1 returns to the main session · /close ends this one", style="dim")
        self._log(note)
        return pane

    async def _on_child_message(self, sid: str, msg: dict) -> None:
        """Handle one JSONL frame from a child session."""
        from novacode_cli.sessions import protocol

        pane = self._pane_for(sid)
        if pane is None:
            return
        kind = msg.get("t")
        previous_status = pane.status

        if kind == "ready":
            pane.status = "idle"
            # Send the task it was spawned with, now that it can accept one.
            task = getattr(self, "_pending_task_for", {}).pop(sid, None)
            if task:
                await self._send_child_prompt(pane, task)
                pane.status = "running"

        elif kind == "ev":
            event = protocol.decode_event(msg)
            if event is not None:
                await self._deliver(pane, event)

        elif kind == "interrupt":
            pane.status = "needs-approval"
            pane.pending_interrupt = msg
            # Remote connection alone never authorizes tool execution.
            remote_tool = (
                pane.remote_turns
                and str(msg.get("kind") or "tool") == "tool"
                and getattr(pane.state.get("session_state"), "auto_approve", False)
            )
            if pane is self._active_pane or remote_tool:
                # @work: returns a Worker, not an awaitable. The handler shows
                # approval modals via push_screen_wait, which Textual only
                # permits inside a worker — child interrupts arrive on the
                # supervisor's raw stdout-reader task, so without @work the
                # push raised NoActiveWorker and every child approval failed
                # closed to a silent reject ("User rejected the tool call").
                self._handle_child_interrupt(pane)
            else:
                # Never steal the screen for a background session: flag the tab
                # and present it when the user switches there.
                with contextlib.suppress(Exception):
                    self.session_state.add_notification(
                        "warn",
                        f"{pane.title}: approval needed",
                        "A background session is waiting for a decision.",
                        "sessions",
                    )

        elif kind == "turn_done":
            pane.status = "idle"
            await self._finish_remote_turn(pane)

        elif kind == "notification":
            from datetime import datetime
            from novacode_cli.states.Session import Notification

            try:
                data = dict(msg["notification"])
                if isinstance(data.get("timestamp"), str):
                    data["timestamp"] = datetime.fromisoformat(data["timestamp"])
                notification = Notification(**data)
                state = pane.state.get("session_state")
                if state is not None:
                    state.notifications.appendleft(notification)
                self._desktop_notification(pane.sid, notification)
            except (KeyError, ValueError, TypeError):
                logger.warning("Invalid child notification frame")

        elif kind == "jobs":
            registry = self._tasks_registry(pane)
            registry.update(msg.get("jobs", []))
            if pane is self._active_pane:
                self._refresh_tasks_bar()

        elif kind == "job_logs":
            future = getattr(pane, "job_log_waiters", {}).get(msg.get("request_id"))
            if future is not None and not future.done():
                future.set_result(str(msg.get("output") or "")[-20000:])

        elif kind == "error":
            await self._deliver(pane, ev.Error(message=str(msg.get("message") or "")))

        elif kind == "exited":
            pane.status = "crashed" if msg.get("crashed") else "exited"
            self._tasks_registry(pane).mark_exited()
            for future in getattr(pane, "job_log_waiters", {}).values():
                if not future.done():
                    future.set_result(None)
            if pane is self._active_pane:
                self._refresh_tasks_bar()
            while pane.remote_turns:
                await self._finish_remote_turn(pane, error=f"✖ Session “{pane.title}” ended.")

        self._sync_child_activity(pane)
        # The 250ms animation timer handles unread counts and stream activity.
        # Only lifecycle transitions require an immediate tab-bar refresh.
        if kind != "ev" or pane.status != previous_status:
            self._refresh_tabs()

    def _sync_child_activity(self, pane) -> None:
        """Let the selected child drive the same status animation as the main tab."""
        if pane.kind != "child":
            return
        busy = pane.status == "running"
        previous = pane.state.get("_turn_active", False)
        if busy and not previous:
            pane.state["_turn_start"] = time.monotonic()
        pane.state["_turn_active"] = busy
        if not busy:
            pane.state["_activity"] = (
                "awaiting approval" if pane.status == "needs-approval" else "ready"
            )
        elif not previous:
            pane.state["_activity"] = "working"
        if pane is getattr(self, "_active_pane", None):
            self._turn_active = busy
            self._turn_start = pane.state.get("_turn_start", 0.0)
            if not busy or not previous:
                self._activity = pane.state["_activity"]
            if busy != previous:
                self._schedule_status_tick()

    @work
    async def _handle_child_interrupt(self, pane) -> None:
        """Present a child's approval in this UI and send the decision back.

        Reuses ``_handle_interrupt`` verbatim, so a spawned session gets exactly
        the same modals as the local one. ``session_state`` is swapped for the
        duration so "remember for this session" and auto-approve apply to the
        CHILD, not the root conversation.
        """
        msg = pane.pending_interrupt
        if msg is None:
            return
        pane.pending_interrupt = None

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        request = ev.InterruptRequest(
            kind=str(msg.get("kind") or "tool"), payload=msg.get("payload"), future=fut
        )
        prev_state = self.session_state
        proxy = pane.state.get("session_state")
        try:
            if proxy is not None:
                self.session_state = proxy
            await self._handle_interrupt(request)
            result = await fut
        except Exception:  # noqa: BLE001 — never leave the child blocked
            result = None
        finally:
            self.session_state = prev_state

        await self._supervisor().reply_interrupt(pane.sid, str(msg.get("id")), result)
        pane.status = "running"
        self._sync_child_activity(pane)
        self._refresh_tabs()

    async def _close_session(self, pane) -> None:
        """Close a spawned session and report what happened to its worktree."""
        from novacode_cli.sessions import worktree as wt

        if pane.kind == "root":
            self._log(Text("The main session can't be closed.", style="#e0af68"))
            return

        await self._supervisor().close(pane.sid)
        while pane.remote_turns:
            await self._finish_remote_turn(pane, error=f"✖ Session “{pane.title}” was closed.")
        await self._close_remote_topics(pane)

        # Worktree cleanup runs only after the process is gone: on Windows a
        # directory a live process holds open cannot be removed. Work is never
        # destroyed — a dirty or committed worktree is kept and reported.
        outcome = ""
        if pane.worktree is not None and pane.branch:
            with contextlib.suppress(Exception):
                repo = wt.repo_root(Path.cwd())
                if repo is not None:
                    outcome = await asyncio.to_thread(wt.remove_worktree, pane.worktree, repo=repo)

        if pane in self._panes:
            self._panes.remove(pane)
        with contextlib.suppress(Exception):
            await pane.scroll.remove()

        if pane is self._active_pane:
            await self._switch_to(self._root_pane)
        self._refresh_tabs()

        note = f"◆ session “{pane.title}” closed"
        if outcome:
            note += f" — worktree {outcome}"
        if pane.branch and "removed" not in outcome:
            note += f" (branch {pane.branch})"
        self._log(Text(note, style="#7aa2f7"))

    def _schedule_prune(self) -> None:
        """Coalesce transcript pruning into one pass per refresh cycle.

        ``_prune_transcript`` is called from ``_log`` and ``_mount`` — i.e. once
        per log line and once per mounted widget. Each call that trips the cap
        does a synchronous ``remove_children``, which makes Textual walk the full
        descendant set of every removed node and then re-layout the transcript.
        A burst of log lines therefore paid that cost repeatedly, and because
        ``_log`` mounts without awaiting, a later prune faced every queued mount
        at once. The stall watchdog recorded 30 freezes totalling ~4,188s with
        the loop blocked in ``_prune_transcript -> remove_children ->
        dom.walk_children``.

        Deferring to ``call_after_refresh`` means N mounts in a burst produce ONE
        prune, and it runs after the pending mounts have been processed so the
        child count it sees is accurate. (Not ``set_timer(0, ...)``: Textual's
        timer divides by its interval and raises ZeroDivisionError at 0.)
        """
        if self._prune_scheduled:
            return
        self._prune_scheduled = True
        self.call_after_refresh(self._run_scheduled_prune)

    def _run_scheduled_prune(self) -> None:
        """Callback for :meth:`_schedule_prune`."""
        self._prune_scheduled = False
        self._prune_transcript()

    def _prune_transcript(self) -> None:
        """Cap the transcript: drop the oldest widgets once it grows too large.

        Skips the in-progress widgets we still hold references to (streaming
        answer/reasoning, open tool/subagent cards, todo, /init tracker) so a
        live turn is never disturbed.
        """
        try:
            tr = self._transcript()
        except NoMatches:
            return
        children = list(tr.children)
        # A widget-count cap alone permits hundreds of large output/markdown
        # bodies. Bound display history by content size as well; agent context
        # and persisted session history are independent of these widgets.
        weights = []
        for child in children:
            weight = 0
            for node in child.walk_children(with_self=True):
                if isinstance(node, OutputLog):
                    weight += node._output_chars
                elif isinstance(node, Static):
                    content = node.content
                    if isinstance(content, (str, Text)):
                        weight += len(content)
                    elif isinstance(content, Markdown):
                        weight += len(content.markup)
            weights.append(weight)
        total_chars = sum(weights)
        over_count = len(children) > _MAX_TRANSCRIPT_WIDGETS
        over_chars = total_chars > _MAX_TRANSCRIPT_CHARS
        if not over_count and not over_chars:
            return
        protected: set[int] = {
            id(w)
            for w in (
                self._stream_msg,
                self._reason_msg,
                self._tool_group,
                *(t[0] for t in self._tool_components.values()),
                *(s[0] for s in self._subagent_widgets.values()),
            )
            if w is not None
        }
        if self._last_tool is not None:
            protected.add(id(self._last_tool[0]))
        to_remove = []
        # Oldest first; stop once we're back at the low-water mark.
        target_count = _TRANSCRIPT_LOW_WATER if over_count else len(children)
        target_chars = _TRANSCRIPT_CHARS_LOW_WATER if over_chars else total_chars
        remaining = len(children)
        for w, weight in zip(children, weights):
            if remaining <= target_count and total_chars <= target_chars:
                break
            if id(w) not in protected:
                to_remove.append(w)
                remaining -= 1
                total_chars -= weight
        if not to_remove:
            return
        # ONE batched removal, not N individual ones. Widget.remove() returns an
        # AwaitRemove and posts its own message; calling it in a loop queued one
        # removal per widget, and since this method is sync none were awaited.
        # Under a fast log burst the queue outran the event loop — the
        # transcript reached 800+ widgets against a 400 cap, and Textual could
        # not drain its pending messages. remove_children() does the whole batch
        # in one operation (measured: an 800-line burst 7.4 s -> 2.9 s).
        try:
            tr.remove_children(to_remove)
        except Exception:  # noqa: BLE001 — pruning must never break rendering
            pass

    def _scroll_end(self, *, force: bool = True) -> None:
        """Scroll the transcript to the newest content.

        Args:
            force: When True (the default) always jump to the bottom. Pass False
                for *automatic* scrolling (new content arriving, a streaming
                repaint) so a user who has scrolled up to read is not yanked back
                down mid-sentence.
        """
        if self._replaying_history:
            return
        if force or self._follow_tail:
            try:
                self._transcript().scroll_end(animate=False)
            except NoMatches:
                pass
        self._update_jump_latest()

    def _update_jump_latest(self, *, measure: bool = False) -> None:
        """Show the "jump to latest" affordance only while not following the tail.

        Driven by the ``_follow_tail`` intent flag rather than the instantaneous
        scroll geometry: content growth moves the bottom without the user moving,
        so geometry alone would flash the button on every streaming repaint.

        Args:
            measure: Recompute the "N lines below" count. Only pass True from a
                real user scroll. Automatic calls (streaming flushes, new content)
                leave the label alone: the count is measured from the bottom, so
                it would otherwise climb on every flush while the user sits still,
                re-rendering the footer ~10x/sec and reading as the scrollbar
                being pushed toward the button.
        """
        try:
            tr = self._transcript()
        except NoMatches:
            return
        try:
            # The row owns visibility (it is the full-width, transparent strip);
            # the inner button owns the text and click/keyboard interaction.
            row = self._w("#jump-latest-row", Horizontal)
            if not self._follow_tail and measure:
                # Show how far back the user is, so the affordance is informative.
                rows = max(0, int(tr.max_scroll_y - tr.scroll_y))
                label = f"↓ Jump to latest  ({rows} lines below)"
                # Only touch the widget when the text actually changes.
                button = self._w("#jump-latest", Button)
                if label != self._jump_latest_label or button.label.plain != label:
                    self._jump_latest_label = label
                    button.label = Text(label, style="bold")
            row.set_class(not self._follow_tail, "active")
        except NoMatches:
            pass

    def on_transcript_scroll_at_end_changed(self, event: TranscriptScroll.AtEndChanged) -> None:
        """Toggle the jump-to-latest button when the transcript reaches/leaves the end."""
        if event.scroll is self._transcript():
            self._update_jump_latest(measure=True)

    def on_transcript_scroll_scrolled(self, event: TranscriptScroll.Scrolled) -> None:
        """Track scroll intent: moving up stops following, reaching the end resumes.

        Only the active pane's scroll region counts — a background session
        scrolling must not change what the visible transcript is doing.
        """
        try:
            if event.scroll is not self._transcript():
                return
        except NoMatches:
            return
        if event.scroll.is_vertical_scroll_end or (event.scroll.max_scroll_y - event.new_y) <= 1:
            # Hiding/showing a pane can clamp its scroll offset downward. At
            # the bottom that is layout, not an instruction to stop following.
            self._follow_tail = True
        elif event.new_y < event.old_y - 1:
            # The viewport moved up: the user is reading history, so stop
            # auto-scrolling. (Content growth never moves scroll_y, so this can
            # only be a real user scroll.)
            self._follow_tail = False
        # A real user scroll: recompute the distance readout.
        self._update_jump_latest(measure=True)

    def action_jump_latest(self) -> None:
        """Scroll the transcript to the newest message (ctrl+end / button click)."""
        self._follow_tail = True
        self._jump_latest_label = ""
        self._scroll_end(force=True)

    @on(Button.Pressed, "#jump-latest")
    def _jump_latest_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_jump_latest()

    async def _mount(self, widget) -> None:
        # Any non-tool content closes the current tool group so transcript order
        # stays correct and the next tool burst starts a fresh group.
        self._close_tool_group()
        await self._transcript().mount(widget)
        self._schedule_prune()
        # Automatic scroll: new content follows the tail only if the user is
        # still following it (see _follow_tail).
        self._scroll_end(force=False)

    def _remote_send(self, text: str) -> None:
        """Send a one-off status line to the remote platform during a remote turn.

        Reserved for low-frequency notices. Per-tool / per-subagent activity goes
        to the session's live status message (see :meth:`_feed_remote`) rather
        than flooding the chat.
        """
        msg = self._remote_msg
        if msg is None:
            return
        try:
            import asyncio

            # Track the task so it isn't garbage-collected mid-send and so
            # exceptions surface rather than vanish (fire-and-forget pitfall).
            task = asyncio.create_task(msg.reply_fn(f"{text}"))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:  # noqa: BLE001
            pass

    # ── remote: one chat, several sessions ───────────────────────────────

    def _remote_sessions(self) -> dict[str, str]:
        """sid -> name of every live session ("main" is the in-process one)."""
        from novacode_cli.remote.routing import ROOT

        out = {ROOT: "main"}
        for pane in getattr(self, "_panes", []):
            if pane.kind == "child" and pane.status not in ("crashed", "exited"):
                out[pane.sid] = pane.title
        return out

    def _remote_label(self, pane) -> str:
        """The session's name for remote messages, or "" when it is the only one."""
        if len(self._remote_sessions()) < 2:
            return ""
        return "main" if pane is None or pane.kind == "root" else pane.title

    def _feed_remote(self, pane, event) -> None:
        """Feed a stream event to the remote status of the session it belongs to."""
        from novacode_cli.remote.status import feed

        try:
            if pane is None or pane.kind == "root":
                if self._remote_status is not None:
                    feed(self._remote_status, event)
                if self._remote_answer is not None:
                    self._remote_answer.feed(event)
                for _target, status, answer in self._local_remote_streams:
                    feed(status, event)
                    answer.feed(event)
                return
            if pane.remote_turns:
                turn = pane.remote_turns[0]
                if turn.status is not None:
                    feed(turn.status, event)
                if getattr(turn, "answer_stream", None) is not None:
                    turn.answer_stream.feed(event)
                if isinstance(event, ev.AssistantMessage) and (event.text or "").strip():
                    turn.answer = event.text  # the last one is the answer
        except Exception:  # noqa: BLE001 — the chat mirror must never break rendering
            pass

    async def _remote_route(self, msg: Any) -> bool:
        """Send ``msg`` to its session. True when it was handled here (another
        session, or a session command); False when it is for the main session.
        """
        from novacode_cli.remote.routing import ROOT, RemoteRouter

        router = getattr(self, "_remote_router", None)
        if router is None:
            router = self._remote_router = RemoteRouter()
        sessions = self._remote_sessions()
        text = (getattr(msg, "text", "") or "").strip()
        low = text.lower()
        route_ctx = getattr(msg, "route", None)
        if not isinstance(route_ctx, dict):
            route_ctx = {}

        async def reply(body: str) -> None:
            route_ctx["sid"] = route_ctx.get("sid") or ROOT
            with contextlib.suppress(Exception):
                await msg.reply_fn(body)

        async def route_prompt() -> bool:
            route = router.resolve(msg, sessions)
            if route is None:
                await reply(
                    "This topic's session has ended. Send /sessions to see what is running."
                )
                return True
            msg.text = route.text
            route_ctx["sid"] = route.sid
            if route.sid == ROOT:
                return False
            pane = self._pane_for(route.sid)
            if pane is None:
                return False
            await self._remote_child_turn(pane, msg)
            return True

        # Captions are prompts, never launch/close confirmations or commands.
        if getattr(msg, "images", None):
            return await route_prompt()

        if (
            getattr(msg, "platform", None) is not None
            and getattr(msg.platform, "value", "") == "telegram"
        ):
            if await self._handle_telegram_launch_message(msg, text):
                return True
            if await self._handle_telegram_tab_close_selection(msg, text):
                return True
            if await self._handle_telegram_project_selection(msg, text):
                return True

        if low in ("/tab close", "/tabs close", "/session close"):
            await self._request_telegram_tab_close(msg)
            return True
        if low in ("/sessions", "/session", "/tabs"):
            await reply(self._remote_sessions_text(msg))
            return True
        if low == "/tab" or low.startswith(("/tab ", "/tab@")):
            command = low.split(maxsplit=1)[1] if len(low.split(maxsplit=1)) > 1 else ""
            if command.split("@", 1)[0] == "close":
                await self._request_telegram_tab_close(msg)
            elif command in ("", "new", "launch"):
                requested = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
                await self._request_telegram_session_launch(msg, requested)
            else:
                await reply("Usage: `/tab` to start a session or `/tab close` to close one.")
            return True
        if low.startswith("/use"):
            name = text[4:].strip()
            sid = router.match(name, sessions) if name else None
            if sid is None:
                await reply(f"No session “{name}”. {self._remote_sessions_text(msg)}")
                return True
            router.default[msg.chat_id] = sid
            await reply(f"● Plain messages here now go to **{sessions[sid]}**.")
            return True
        if low.startswith("/new"):
            name, _, task = text[4:].strip().partition(":")
            if not name.strip():
                await reply("Usage: `/new <name>` or `/new <name>: <task>`")
                return True
            await reply(f"◌ Starting session **{name.strip()}**…")
            await self.spawn_session(name.strip(), task.strip())
            return True

        if low.startswith("/session launch"):
            await self._request_telegram_session_launch(msg, text[len("/session launch") :].strip())
            return True

        natural_launch = self._parse_natural_launch_intent(text)
        if natural_launch:
            phrase, task = natural_launch
            await self._request_telegram_session_launch(msg, phrase, task=task)
            return True

        return await route_prompt()

    async def _handle_telegram_launch_message(self, msg: Any, text: str) -> bool:
        """Consume scoped launch confirmations before they reach an agent turn."""
        match = re.fullmatch(r"\s*(Launch|Cancel)\s+([a-f0-9]{12})\s*", text, re.IGNORECASE)
        if not match:
            return False
        request_id = match.group(2).lower()
        pending = getattr(self, "_remote_launch_approvals", {}).get(request_id)
        if pending is None:
            body = "That launch request has already completed or expired. Send `/tab` to start a new one."
            if NovaApp._has_telegram_picker(self, msg):
                await msg.reply_fn(body)
            else:
                await NovaApp._clear_telegram_keyboard(self, msg, body)
            return True
        if (
            str(msg.sender_id) != pending["sender_id"]
            or str(msg.chat_id) != pending["chat_id"]
            or msg.thread_id != pending["thread_id"]
        ):
            await msg.reply_fn(
                "This confirmation belongs to another requester or topic. Send `/tab` for your own request."
            )
            return True
        if time.monotonic() >= pending["expires"]:
            self._remote_launch_approvals.pop(request_id, None)
            with contextlib.suppress(Exception):
                await NovaApp._clear_telegram_keyboard(
                    self, msg, f"Launch request {request_id} expired. Send `/tab` again."
                )
            return True
        self._remote_launch_approvals.pop(request_id, None)
        if match.group(1).lower() == "cancel":
            await NovaApp._clear_telegram_keyboard(
                self, msg, f"Cancelled launch request {request_id}."
            )
            return True
        # Consume permission and retire the keyboard before awaiting startup.
        # A second click cannot launch twice or leave these buttons active.
        await NovaApp._clear_telegram_keyboard(
            self, msg, "Launch approved. Starting the session tab…"
        )
        try:
            request = pending["request"]
            from novacode_cli.sessions.launch import validate_approved_folder

            folder = await asyncio.to_thread(validate_approved_folder, request["folder"])
            pane = await self.spawn_session(
                request["name"],
                request["task"],
                directory=folder,
                auto_approve=request.get("auto_approve", False),
            )
            if pane is None:
                raise RuntimeError("Nova could not start a tab for that folder.")
            router = getattr(self, "_remote_router", None)
            topic = router.topic_of(msg.chat_id, pane.sid) if router else None
            link = ""
            if topic:
                chat = str(msg.chat_id)
                internal = chat[4:] if chat.startswith("-100") else chat
                link = f"\nTelegram topic: https://t.me/c/{internal}/{topic}"
            await msg.reply_fn(
                f"Opened session tab **{pane.title}** for `{folder}`. "
                f"The initial task is queued for this session.{link}"
            )
        except Exception as exc:
            await msg.reply_fn(f"Could not launch the session: {exc}")
        return True

    async def _request_telegram_session_launch(
        self, msg: Any, requested_folder: str, *, task: str = ""
    ) -> None:
        """Show approved project buttons for a Telegram session launch request."""
        from novacode_cli.sessions.launch import approved_projects

        name = None
        NovaApp._reset_telegram_pickers(self, msg)
        if requested_folder:
            task_match = re.search(
                r"(?:^|\s)--task(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|(.+?))(?=\s+--|$)",
                requested_folder,
            )
            name_match = re.search(
                r"(?:^|\s)--name(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|([^\s]+))",
                requested_folder,
            )
            if task_match and not task:
                task = next((part for part in task_match.groups() if part is not None), "")
            if name_match:
                name = next((part for part in name_match.groups() if part is not None), None)

        projects = await asyncio.to_thread(approved_projects)
        if not projects:
            await NovaApp._clear_telegram_keyboard(
                self, msg, "There are no existing Nova-approved project folders to choose from."
            )
            return
        if msg.sender_id is None:
            await msg.reply_fn(
                "Telegram did not provide a stable sender ID; launch approval is unavailable."
            )
            return

        labels = {f"{index}. {path.name}": str(path) for index, path in enumerate(projects, 1)}
        key = self._telegram_picker_key(msg)
        pickers = getattr(self, "_remote_project_pickers", None)
        if pickers is None:
            pickers = self._remote_project_pickers = {}
        pickers[key] = {
            "choices": labels,
            "task": task.strip(),
            "name": name,
            "expires": time.monotonic() + 300,
        }
        hint = f"Your task: {task.strip()}\n\n" if task.strip() else ""
        await self._post_telegram_keyboard(
            msg,
            f"{hint}Choose an approved project folder for the new session tab:",
            list(labels),
        )
        NovaApp._arm_telegram_picker_expiry(self, msg, pickers, key)

    @staticmethod
    def _telegram_picker_key(msg: Any) -> tuple[str, str, str]:
        return (str(msg.sender_id), str(msg.chat_id), str(msg.thread_id or ""))

    def _reset_telegram_pickers(self, msg: Any) -> None:
        """A new picker supersedes earlier choices in the same request scope."""
        key = NovaApp._telegram_picker_key(msg)
        for attr in ("_remote_project_pickers", "_remote_tab_closers"):
            getattr(self, attr, {}).pop(key, None)
        approvals = getattr(self, "_remote_launch_approvals", {})
        for request_id, record in list(approvals.items()):
            if (record["sender_id"], record["chat_id"], str(record["thread_id"] or "")) == key:
                approvals.pop(request_id, None)

    def _has_telegram_picker(self, msg: Any) -> bool:
        key = NovaApp._telegram_picker_key(msg)
        if any(
            key in getattr(self, attr, {})
            for attr in ("_remote_project_pickers", "_remote_tab_closers")
        ):
            return True
        return any(
            (record["sender_id"], record["chat_id"], str(record["thread_id"] or "")) == key
            for record in getattr(self, "_remote_launch_approvals", {}).values()
        )

    def _arm_telegram_picker_expiry(self, msg: Any, records: dict, key: Any) -> None:
        """Expire idle keyboards; an old timer cannot clear a replacement picker."""
        if not getattr(self, "is_running", False):
            return
        record = records.get(key)
        if record is None:
            return

        async def expire() -> None:
            if records.get(key) is not record:
                return
            records.pop(key, None)
            with contextlib.suppress(Exception):
                await NovaApp._clear_telegram_keyboard(
                    self, msg, "Tab picker expired. Send `/tab` or `/tab close` again."
                )

        self.set_timer(max(0.01, record["expires"] - time.monotonic()), expire)

    async def _post_telegram_keyboard(self, msg: Any, text: str, choices: list[str]) -> None:
        """Send a keyboard through the current Telegram bridge, with a text fallback."""
        route = getattr(msg, "route", {}) or {}
        for bridge in self._remote_telegram_bridges():
            if str(bridge._config.chat_id) == str(msg.chat_id):
                rows = [choices[index : index + 2] for index in range(0, len(choices), 2)]
                if await bridge.post_keyboard(
                    text,
                    rows,
                    thread_id=msg.thread_id,
                    sid=route.get("sid") or "root",
                    reply_to_message_id=getattr(msg, "message_id", None),
                ):
                    return
        fallback = text + "\n\n" + "\n".join(f"• {choice}" for choice in choices)
        await msg.reply_fn(fallback)

    async def _clear_telegram_keyboard(self, msg: Any, text: str) -> None:
        route = getattr(msg, "route", {}) or {}
        get_bridges = getattr(self, "_remote_telegram_bridges", lambda: [])
        for bridge in get_bridges():
            if str(bridge._config.chat_id) != str(msg.chat_id):
                continue
            try:
                if await bridge.remove_keyboard(
                    text,
                    thread_id=msg.thread_id,
                    sid=route.get("sid") or "root",
                    reply_to_message_id=getattr(msg, "message_id", None),
                ):
                    return
            except Exception:
                logger.warning("Could not remove Telegram tab picker", exc_info=True)
        await msg.reply_fn(text)

    async def _handle_telegram_project_selection(self, msg: Any, text: str) -> bool:
        """Accept a project button only from the requester in the originating topic."""
        if getattr(msg, "sender_id", None) is None:
            return False
        key = self._telegram_picker_key(msg)
        pending = getattr(self, "_remote_project_pickers", {}).get(key)
        if pending is None or text not in pending["choices"]:
            return False
        if time.monotonic() >= pending["expires"]:
            self._remote_project_pickers.pop(key, None)
            await NovaApp._clear_telegram_keyboard(
                self, msg, "Project picker expired. Send `/tab` again."
            )
            return True
        self._remote_project_pickers.pop(key, None)
        await self._queue_telegram_launch_approval(
            msg,
            pending["choices"][text],
            name=pending["name"],
            task=pending["task"],
        )
        return True

    async def _request_telegram_tab_close(self, msg: Any) -> None:
        """Show running child tabs as Telegram buttons for the requesting user."""
        NovaApp._reset_telegram_pickers(self, msg)
        if getattr(msg, "sender_id", None) is None:
            await msg.reply_fn(
                "Telegram did not provide a stable sender ID; tab closing is unavailable."
            )
            return
        panes = [
            pane
            for pane in getattr(self, "_panes", [])
            if pane.kind == "child" and pane.status not in ("crashed", "exited")
        ]
        if not panes:
            await NovaApp._clear_telegram_keyboard(
                self, msg, "There are no running session tabs to close."
            )
            return
        choices = {f"{index}. {pane.title}": pane.sid for index, pane in enumerate(panes, 1)}
        key = self._telegram_picker_key(msg)
        closers = getattr(self, "_remote_tab_closers", None)
        if closers is None:
            closers = self._remote_tab_closers = {}
        closers[key] = {"choices": choices, "expires": time.monotonic() + 300}
        await self._post_telegram_keyboard(
            msg,
            "Choose the running session tab to close:",
            list(choices),
        )
        NovaApp._arm_telegram_picker_expiry(self, msg, closers, key)

    async def _handle_telegram_tab_close_selection(self, msg: Any, text: str) -> bool:
        """Close only the selected live tab, scoped to its requester and topic."""
        if getattr(msg, "sender_id", None) is None:
            return False
        key = self._telegram_picker_key(msg)
        pending = getattr(self, "_remote_tab_closers", {}).get(key)
        if pending is None or text not in pending["choices"]:
            return False
        if time.monotonic() >= pending["expires"]:
            self._remote_tab_closers.pop(key, None)
            await NovaApp._clear_telegram_keyboard(
                self, msg, "Session close picker expired. Send `/tab close` again."
            )
            return True
        self._remote_tab_closers.pop(key, None)
        sid = pending["choices"][text]
        pane = self._pane_for(sid)
        if pane is None or pane.kind != "child" or pane.status in ("crashed", "exited"):
            await NovaApp._clear_telegram_keyboard(
                self, msg, "That session tab has already stopped."
            )
            return True
        title = pane.title
        await NovaApp._clear_telegram_keyboard(self, msg, f"Closing session tab {title}…")
        await self._close_session(pane)
        return True

    async def _queue_telegram_launch_approval(
        self, msg: Any, folder: str, *, name: str | None, task: str
    ) -> None:
        from pathlib import Path
        from novacode_cli.path_approval import PathApprovalManager
        from novacode_cli.sessions.launch import prepare_launch

        try:
            request = prepare_launch(folder, name, task, str(Path.cwd()))
        except Exception as exc:
            await NovaApp._clear_telegram_keyboard(
                self, msg, f"Could not prepare that project session: {exc}"
            )
            return
        if not PathApprovalManager().is_path_approved(Path(request["folder"])):
            await NovaApp._clear_telegram_keyboard(
                self, msg, "That folder is no longer Nova-approved. Choose a project again."
            )
            return
        source = None
        router = getattr(self, "_remote_router", None)
        if router is not None:
            route = router.resolve(msg, self._remote_sessions())
            if route is not None:
                source = self._pane_for(route.sid)
        source_state = (
            source.state.get("session_state")
            if source is not None and source.kind == "child"
            else self._remote_owner_state()
        )
        request["auto_approve"] = bool(getattr(source_state, "auto_approve", False))
        request["telegram"] = self._launch_telegram_metadata(msg.chat_id, msg.thread_id)
        if request["telegram"] is None:
            await NovaApp._clear_telegram_keyboard(
                self,
                msg,
                "I could not verify the Telegram bot configuration for this chat, so I cannot launch a remotely connected session.",
            )
            return
        request_id = uuid.uuid4().hex[:12]
        NovaApp._reset_telegram_pickers(self, msg)
        pending = getattr(self, "_remote_launch_approvals", None)
        if pending is None:
            pending = self._remote_launch_approvals = {}
        while request_id in pending:
            request_id = uuid.uuid4().hex[:12]
        request["request_id"] = request_id
        pending[request_id] = {
            "request": request,
            "sender_id": str(msg.sender_id),
            "chat_id": str(msg.chat_id),
            "thread_id": msg.thread_id,
            "expires": time.monotonic() + 300,
        }
        summary = (
            f"Approve opening a new Nova session tab?\n"
            f"Folder: {request['folder']}\nName: {request['name']}\n"
            f"Initial task: {request['task'] or '(none)'}\n"
            f"Tool approvals: {'auto-approve' if request['auto_approve'] else 'ask'}\n"
            "Workspace trust: already approved\n\n"
            "Choose Launch or Cancel. This confirmation expires in five minutes."
        )
        await self._post_telegram_keyboard(
            msg, summary, [f"Launch {request_id}", f"Cancel {request_id}"]
        )
        NovaApp._arm_telegram_picker_expiry(self, msg, pending, request_id)

    def _remote_sessions_text(self, msg: Any) -> str:
        from novacode_cli.remote.routing import ROOT

        router = getattr(self, "_remote_router", None)
        chat = getattr(msg, "chat_id", None)
        current = router.default.get(chat, ROOT) if router else ROOT
        lines = ["**Sessions**"]
        for pane in getattr(self, "_panes", []):
            sid = ROOT if pane.kind == "root" else pane.sid
            name = "main" if pane.kind == "root" else pane.title
            where = " · has its own topic" if router and router.topic_of(chat, sid) else ""
            mark = " ← default here" if sid == current else ""
            lines.append(f"- **{name}** · {pane.status}{where}{mark}")
        lines.append(
            "\nTalk to one: post in its topic, reply to its message, "
            "`@name <text>`, or `/use <name>`. Start with `/tab`; close with `/tab close`."
        )
        return "\n".join(lines)

    async def _send_child_prompt(self, pane, text: str, *, images=None):
        """Send the tab's explicit approval preference with each prompt."""
        state = pane.state.get("session_state")
        options = {"auto_approve": bool(getattr(state, "auto_approve", False))}
        if images:
            options["images"] = images
        return await self._supervisor().send_prompt(pane.sid, text, **options)

    async def _remote_child_turn(self, pane, msg: Any) -> None:
        """Run a remote prompt in a spawned session; answer at its turn_done."""
        from types import SimpleNamespace

        from novacode_cli.remote.status import RemoteStatusLine

        if pane.child is None or pane.status in ("crashed", "exited"):
            with contextlib.suppress(Exception):
                await msg.reply_fn(f"✖ Session “{pane.title}” is not running.")
            return
        with contextlib.suppress(Exception):
            await self._add_message(
                Text(f"📡 {msg.user_name} → {pane.title}", style="bold cyan"),
                "user",
                Text(msg.text),
            )
        turn = SimpleNamespace(
            msg=msg,
            status=None,
            answer="",
            answer_stream=None,
        )
        if getattr(msg, "edit_fn", None) is not None:
            turn.status = RemoteStatusLine(msg.edit_fn, label=self._remote_label(pane))
        if getattr(msg, "answer_edit_fn", None) is not None:
            from novacode_cli.remote.streaming import RemoteAnswerStream

            turn.answer_stream = RemoteAnswerStream(msg.answer_edit_fn)
        pane.remote_turns.append(turn)
        if (
            await self._send_child_prompt(pane, msg.text, images=getattr(msg, "images", None))
            is None
        ):
            pane.remote_turns.remove(turn)
            with contextlib.suppress(Exception):
                await msg.reply_fn(f"✖ Session “{pane.title}” is no longer running.")
            return
        if turn.status is not None:
            turn.status.start()
        if turn.answer_stream is not None:
            turn.answer_stream.start()
        if len(pane.remote_turns) > 1:
            with contextlib.suppress(Exception):
                await msg.reply_fn(
                    f"◌ Queued for **{pane.title}** behind {len(pane.remote_turns) - 1} more."
                )
        self._remote_react("🤔", msg)
        pane.status = "running"
        self._sync_child_activity(pane)
        self._refresh_tabs()

    async def _finish_remote_turn(self, pane, error: str | None = None) -> None:
        """Settle the oldest remote turn of a spawned session and send its answer."""
        if not pane.remote_turns:
            return
        turn = pane.remote_turns.popleft()
        if turn.status is not None:
            with contextlib.suppress(Exception):
                await turn.status.finalize(outcome="failed" if error else "done")
        reply = error or turn.answer or "✅ Task completed."
        if self._remote_label(pane) and not error:
            reply = f"**[{pane.title}]** {reply}"
        with contextlib.suppress(Exception):
            stream = getattr(turn, "answer_stream", None)
            if stream is None or not await stream.finalize(reply):
                await turn.msg.reply_fn(reply)
        self._remote_react("✖" if error else "✅", turn.msg)

    def _remote_telegram_bridges(self) -> list:
        from novacode_cli.remote.bridge import RemotePlatform

        mgr = getattr(self.session_state, "_remote_bridge_manager", None)
        if mgr is None:
            root = getattr(self, "_root_pane", None)
            state = root.state.get("session_state") if root is not None else None
            mgr = getattr(state, "_remote_bridge_manager", None)
        try:
            return mgr.bridges(RemotePlatform.TELEGRAM) if mgr is not None else []
        except Exception:  # noqa: BLE001
            return []

    def _launch_telegram_metadata(
        self, chat_id: str | int | None = None, thread_id: int | None = None
    ) -> dict | None:
        """Capture bot identity and destination only; never copy its token."""
        import hashlib

        from novacode_cli.remote.config import get_telegram_config

        config = get_telegram_config() or {}
        token = config.get("token")
        configured_chat = config.get("chat_id")
        for bridge in self._remote_telegram_bridges():
            bridge_chat = bridge._config.chat_id
            if chat_id is not None and str(bridge_chat) != str(chat_id):
                continue
            token = bridge._config.token
            configured_chat = bridge_chat
            break
        if not token or not configured_chat:
            return None
        if chat_id is not None and str(configured_chat) != str(chat_id):
            return None
        return {
            "chat_id": str(configured_chat),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "origin_thread_id": thread_id,
        }

    def _remote_owner_state(self):
        """The root owns the connection even while a child tab is visible."""
        root = getattr(self, "_root_pane", None)
        if root is not None and root is not getattr(self, "_active_pane", root):
            return root.state.get("session_state", self.session_state)
        return self.session_state

    async def _sync_remote_topics(self) -> None:
        for pane in getattr(self, "_panes", []):
            if pane.kind == "root" or pane.status not in ("crashed", "exited"):
                await self._open_remote_topics(pane)

    async def _open_remote_topics(self, pane) -> None:
        """Bind every live session to its own stable Telegram topic."""
        from novacode_cli.remote.routing import RemoteRouter

        router = getattr(self, "_remote_router", None)
        if router is None:
            router = self._remote_router = RemoteRouter()
        state = (
            self._remote_owner_state() if pane.kind == "root" else pane.state.get("session_state")
        )
        session_id = str(getattr(state, "session_id", "") or pane.sid)
        for bridge in self._remote_telegram_bridges():
            try:
                tid = await bridge.ensure_session_topic(pane.sid, session_id, pane.title)
                if tid is None:
                    self.notify(
                        "Enable Telegram Topics (or private bot topic mode), and grant Manage Topics permission in groups.",
                        severity="warning",
                    )
                    continue
                previous = router.topic_of(bridge._config.chat_id, pane.sid)
                router.topics[(bridge._config.chat_id, pane.sid)] = tid
                if previous != tid:
                    await bridge.post(
                        f"Session **{session_id}** connected. Messages in this topic go to **{pane.title}**.",
                        thread_id=tid,
                        sid=pane.sid,
                    )
            except Exception as error:
                self.notify(
                    f"Telegram session topic could not connect: {error}", severity="warning"
                )

    async def _close_remote_topics(self, pane) -> None:
        router = getattr(self, "_remote_router", None)
        if router is None:
            return
        gone = router.forget(pane.sid)
        for bridge in self._remote_telegram_bridges():
            for chat, tid in gone:
                if chat != bridge._config.chat_id:
                    continue
                with contextlib.suppress(Exception):
                    await bridge.post(f"○ Session **{pane.title}** closed.", thread_id=tid)
                    await bridge.close_topic(tid)

    async def _defer_remote_image_turn(self, msg: Any, queue: Any) -> None:
        """Keep attachments intact until the root is ready for another turn."""

        async def enqueue_when_ready() -> None:
            while True:
                root = getattr(self, "_root_pane", None)
                busy = (
                    self._turn_active
                    if root is self._active_pane or root is None
                    else root.state.get("_turn_active", False)
                )
                lock = getattr(self._remote_owner_state(), "_remote_message_lock", None)
                if not busy and (lock is None or not lock.locked()):
                    await queue.put(msg)
                    return
                await asyncio.sleep(0.1)

        task = asyncio.create_task(enqueue_when_ready())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        if not msg.route.get("image_queued"):
            msg.route["image_queued"] = True
            with contextlib.suppress(Exception):
                await msg.reply_fn(
                    "Image queued — Nova will inspect it after the current task finishes."
                )

    async def _remote_steer_drain(self, queue: Any) -> None:
        """While a remote turn runs, treat further remote messages as live steers.

        Lets a remote user "add extra stuff to the previous prompt": a message
        (or ``/steer …``) arriving mid-turn is injected as a live steer the
        running agent picks up at its next step, instead of queuing a whole new
        turn behind the current one. Other slash commands get a "busy" note.
        Cancelled when the turn ends.
        """
        while True:
            try:
                m = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                # Another session's message, or a session command: not a steer.
                if await self._remote_route(m):
                    continue
                if getattr(m, "images", None):
                    await self._defer_remote_image_turn(m, queue)
                    continue
                if (
                    self._remote_question_future is not None
                    and not self._remote_question_future.done()
                    and not (getattr(m, "text", "") or "").lstrip().startswith("/")
                ):
                    react_fn = getattr(m, "react_fn", None)
                    if react_fn is not None:
                        try:
                            await react_fn("📥")
                        except Exception:  # noqa: BLE001
                            pass
                    self._remote_question_future.set_result(m)
                    continue

                text = (getattr(m, "text", "") or "").strip()
                low = text.lower()
                if low.startswith("/steer"):
                    text = text[len("/steer") :].strip()
                elif text.startswith("/"):
                    reply_fn = getattr(m, "reply_fn", None)
                    if reply_fn is not None:
                        try:
                            await reply_fn(
                                "◐ Busy with the current task — send "
                                "`/steer <text>` (or just text) to add to it."
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                if not text:
                    continue
                self._add_live_steer(text)
                react_fn = getattr(m, "react_fn", None)
                reply_fn = getattr(m, "reply_fn", None)
                if react_fn is not None:
                    try:
                        await react_fn("↗")
                    except Exception:  # noqa: BLE001
                        pass
                elif reply_fn is not None:
                    try:
                        await reply_fn(f"↗ Added to the running task: {text}")
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                with suppress(ValueError):
                    queue.task_done()
                    pass

    def _remote_react(self, emoji: str, msg: Any = None) -> None:
        """Add a reaction emoji to the remote user's message (best-effort).

        ``msg`` defaults to the active remote message, but callers can pass it
        explicitly (e.g. the error handler, which runs after ``_remote_msg`` has
        already been cleared).
        """
        msg = msg if msg is not None else self._remote_msg
        if msg is None or getattr(msg, "react_fn", None) is None:
            return
        try:
            import asyncio

            task = asyncio.create_task(msg.react_fn(emoji))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:  # noqa: BLE001
            pass

    def _log(self, renderable: Any) -> None:
        """Mount an ancillary line (errors, command output, notices)."""
        self._close_tool_group()
        self._transcript().mount(Static(renderable, classes="logline"))
        self._schedule_prune()
        # Automatic scroll: don't yank a user who is reading history.
        self._scroll_end(force=False)

    # _init step-tracker widget removed — /init progress is now shown via _log only.

    def _user_label(self, name: str = "You") -> Text:
        """The transcript header for the user's own turn, in the theme's colours.

        Was a literal ``Text("You", style="bold cyan")`` at every call site. ANSI
        cyan is not themeable, so ``/theme`` left the user's own label on a fixed
        terminal colour while everything around it moved.
        """
        from novacode_cli.tui.palette import user_label_style

        return Text(name, style=user_label_style(self._palette))

    def _agent_label(self, name: str, agent_color: str | None = None) -> Text:
        """The transcript header for the agent's own turn.

        Follows the theme, unless the assistant is a named subagent with an
        explicit identity colour from its ``agent.md`` frontmatter — that colour
        is what keeps such a subagent visually distinct from the main agent.

        The main agent is recognised by *name* rather than by its caller-supplied
        colour, because the agent loop still hands down a legacy hardcoded green
        (``COLORS["success"]``) for it. Trusting that would reintroduce the exact
        bug this replaced: a pinned colour that ``/theme`` can never move.
        """
        from novacode_cli.tui.palette import agent_label_style

        return Text(name, style=agent_label_style(self._palette, name, agent_color))

    def _journal(self, entry: dict[str, Any]) -> None:
        """Record a transcript item the message history does not hold.

        See ``session/transcript_journal.py``. A no-op during a replay (the
        entries being rendered came *from* the journal) and without a session.
        """
        if self._replaying:
            return
        sessions_dir = getattr(self.session_manager, "sessions_dir", None)
        session_id = getattr(self.session_state, "session_id", None)
        if not sessions_dir or not session_id:
            return
        from novacode_cli.session import transcript_journal

        transcript_journal.append(sessions_dir, str(session_id), entry)

    async def _add_message(self, label: Text, role_class: str, body: Any) -> ChatMessage:
        if role_class == "user":
            # A marker per prompt: what lets journal entries be put back after
            # the right turn on resume.
            text = getattr(body, "markup", None) or getattr(body, "plain", None) or ""
            self._journal({"k": "user", "text": str(text)})
        msg = ChatMessage(label, role_class)
        await self._mount(msg)
        msg.update_body(body)
        animate_entrance(msg, "slide")
        return msg

    @staticmethod
    def _message_text(msg: Any) -> str:
        """Extract displayable text from a LangChain message's content."""
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content)

    def _format_breadcrumbs(self, path: Path) -> str:
        """Convert an absolute path into a condensed breadcrumb format.

        Example: B:\\Summer Project 2026\\Nova-Code\\nova-code-cli
                 -> .../Nova-Code/nova-code-cli
        """
        parts = path.parts
        if len(parts) <= 3:
            return str(path)

        # Keep the last 2 segments and prefix with .../
        breadcrumb = "/".join(parts[-2:])
        return f".../{breadcrumb}"

    @work
    async def _replay_history(self) -> None:
        """Replay restored conversation turns into the transcript on resume.

        Renders the prior Human/AI turns *and* the tool calls they made (with
        their results) so a resumed session shows the same transcript it had
        before. The agent's own state is restored separately via the
        checkpointer / continuation prompt.
        """
        self._replaying = True
        try:
            await self._replay_history_inner()
        finally:
            self._replaying = False

    async def _replay_history_inner(self) -> None:
        from novacode_cli.session.imported_context import MESSAGE_ID, ImportedContext
        from novacode_cli.tui.session_transcript import show_imported_history
        from novacode_cli.compaction import is_compaction_summary
        from novacode_cli.core.streaming import is_internal_context_text
        from novacode_cli.session import transcript_journal

        # The whole saved history is restored into the agent, but only its tail
        # is drawn: the transcript holds ~200 widgets and older ones would be
        # mounted just to be pruned.
        msgs = self._restored_messages[-_REPLAY_MAX_MESSAGES:]

        journal: list[dict[str, Any]] = []
        sessions_dir = getattr(self.session_manager, "sessions_dir", None)
        session_id = getattr(self.session_state, "session_id", None)
        if sessions_dir and session_id:
            journal = await asyncio.to_thread(
                transcript_journal.load, sessions_dir, str(session_id)
            )
            try:
                imported = await asyncio.to_thread(
                    ImportedContext.load, sessions_dir, str(session_id)
                )
            except (OSError, ValueError, KeyError, TypeError):
                imported = None
            if imported is not None:
                await show_imported_history(self, imported.session)
        if not msgs and not journal:
            return

        def shown_text(m: Any) -> str:
            if getattr(m, "id", None) == MESSAGE_ID:
                return ""
            if getattr(m, "additional_kwargs", {}).get("lc_source") == "pinned_skill":
                return ""
            text = self._message_text(m).strip()
            if text and (is_compaction_summary(text) or is_internal_context_text(text)):
                return ""
            return text

        humans = [t for m in msgs if getattr(m, "type", "") == "human" and (t := shown_text(m))]
        per_turn, leading = transcript_journal.place(journal, humans)
        turn = -1  # index into `humans` of the turn being rendered
        for entry in leading:
            await self._replay_journal_entry(entry)

        # Tool results arrive as separate ToolMessages; index them by call id so
        # an AIMessage's tool_calls can be paired with their output.
        results: dict[str, Any] = {}
        for m in msgs:
            if getattr(m, "type", "") == "tool":
                cid = getattr(m, "tool_call_id", None)
                if cid:
                    results[cid] = m

        # Only render real conversation turns, oldest first.
        shown = 0
        for m in msgs:
            role = getattr(m, "type", "") or ""
            if getattr(m, "id", None) == MESSAGE_ID:
                continue
            if getattr(m, "additional_kwargs", {}).get("lc_source") == "pinned_skill":
                snapshot = m.additional_kwargs.get("skill", {})
                await self._add_message(
                    Text("Skill", style="bold cyan"),
                    "system",
                    Text(f"Pinned: {snapshot.get('name', 'skill')}", style="dim cyan"),
                )
                continue
            text = self._message_text(m).strip()
            # /compact rewrites history into a single synthetic HumanMessage
            # holding the summary (compaction.py). Replaying it verbatim shows
            # the whole summarized context as though the USER had typed it.
            if text and (is_compaction_summary(text) or is_internal_context_text(text)):
                continue
            if role == "human":
                if not text:
                    continue
                # The previous turn is complete: show what the user ran after it.
                if turn >= 0:
                    for entry in per_turn[turn]:
                        await self._replay_journal_entry(entry)
                        shown += 1
                turn += 1
                await self._add_message(self._user_label(), "user", Markdown(text))
                shown += 1
            elif role == "ai":
                # Tool calls ride on the AIMessage; replay them into the
                # condensed tool group before the (possibly empty) prose.
                for tc in getattr(m, "tool_calls", None) or []:
                    await self._replay_tool_call(tc, results)
                if text:
                    await self._add_message(self._agent_label("Nova"), "nova", Markdown(text))
                    shown += 1
        if 0 <= turn < len(per_turn):
            for entry in per_turn[turn]:
                await self._replay_journal_entry(entry)
                shown += 1
        shown += len(leading)
        if shown:
            self._log(
                Text(
                    f"⟲ Resumed — {shown} earlier message(s) restored above",
                    style="dim",
                )
            )

    async def _replay_journal_entry(self, entry: dict[str, Any]) -> None:
        """Draw one restored journal entry (a ``!`` command or a background job)."""
        kind = entry.get("k")
        pal = self._palette
        self._close_tool_group()
        if kind == "bash":
            exit_code = entry.get("exit")
            row = Text()
            row.append("! ", style=f"bold {pal.accent}")
            row.append(str(entry.get("cmd", "")))
            if not entry.get("fg", True):
                row.append("   background", style="dim")
            if exit_code is None:
                row.append("   cancelled", style=f"bold {pal.error}")
            elif exit_code != 0:
                row.append(f"   exit {exit_code}", style=f"bold {pal.error}")
            log_widget = OutputLog(
                classes="bash-inline-log",
                highlight=False,
                markup=False,
                wrap=True,
                max_lines=_LOG_MAX_LINES,
            )
            await self._transcript().mount(
                Vertical(Static(row, classes="bash-inline-head"), log_widget, classes="bash-inline")
            )
            lines = entry.get("lines") or []
            for n, line in enumerate(lines or ["(no output)"]):
                out = Text("  └  " if n == 0 else "     ", style="dim")
                out.append(str(line), style="" if lines else "dim")
                log_widget.write(out)
        elif kind == "bgagent":
            ok = entry.get("status") == "done"
            head = Text()
            head.append("● " if ok else "✖ ", style=pal.success if ok else pal.error)
            head.append(
                f"background agent · {self._oneline(str(entry.get('prompt', '')))}", style="bold"
            )
            self._log(head)
            summary = str(entry.get("summary") or "").strip()
            if summary:
                await self._add_message(self._agent_label("Nova"), "nova", Markdown(summary))

    async def _replay_tool_call(self, tc: dict, results: dict[str, Any]) -> None:
        """Render one restored tool call (and its result) into the tool group.

        Mirrors the live path: a condensed line per call under its category
        heading, marked done/failed from the paired ToolMessage.
        """
        name = tc.get("name", "") or "tool"
        args = tc.get("args", {}) or {}
        call_id = tc.get("id") or None
        try:
            from novacode_cli.ui.ui_elements import format_tool_display

            base = format_tool_display(name, args)
        except Exception:  # noqa: BLE001 — a display helper must never break replay
            base = name
        await self._ensure_tool_group()
        self._add_tool_group_call(call_id, base, name)
        result = results.get(call_id) if call_id else None
        if result is None:
            # No paired result (e.g. the turn was interrupted) — leave it done
            # rather than showing a perpetual spinner.
            self._mark_tool_group_result(call_id, is_error=False, detail="")
            return
        status = getattr(result, "status", "success")
        is_error = status != "success"
        detail = self._oneline(self._message_text(result))
        self._mark_tool_group_result(call_id, is_error=is_error, detail=detail)

    def _reset_streaming(self) -> None:
        """Drop any in-progress streaming/reasoning widgets at a turn boundary."""
        for ref in (self._stream_msg, self._reason_msg):
            if ref is not None:
                try:
                    ref.remove()  # fire-and-forget
                except Exception:  # noqa: BLE001
                    pass
        self._stream_msg = None
        self._reason_msg = None
        self._live_buf = ""
        self._reasoning_buf = ""
        self._stream_flush_scheduled = False
        self._tool_components.clear()
        self._last_tool = None
        self._close_tool_group()
        self._subagent_widgets.clear()
        self._subagent_count = 0
        self._subagent_tool_to_task.clear()
        # NB: the docked checklist is deliberately NOT cleared here — this
        # runs at the start of every turn, and the point of docking is that
        # the list survives. /clear and /resume clear it explicitly.
        self._current_assistant_id = None
        self._accumulated_reply = ""

    @staticmethod
    def _rope_join(parts: list[str]) -> str:
        """Collapse accumulated fragments into one string, in place.

        Joining is deferred until a read, so a stream pays one O(n) copy per
        repaint instead of one per delta, and collapsing the list keeps later
        reads O(1). Appending to a list is amortised O(1), which is the point.
        """
        if len(parts) > 1:
            joined = "".join(parts)
            parts.clear()
            parts.append(joined)
        return parts[0] if parts else ""

    @staticmethod
    def _rope_tail(parts: list[str], max_chars: int) -> str:
        """Read the live preview without copying the growing full transcript."""
        tail = []
        remaining = max_chars
        for part in reversed(parts):
            if remaining <= 0:
                break
            tail.append(part[-remaining:])
            remaining -= len(tail[-1])
        return "".join(reversed(tail))

    # Streamed text is appended one fragment per model delta, so this is the
    # hottest write in the TUI. A plain `self._buf += fragment` is quadratic:
    # CPython's in-place-resize optimisation only applies to a *local* with a
    # refcount of 1, never to an attribute, so each delta copies the whole
    # buffer. Measured on this machine (10-char deltas, best of 3):
    #
    #     2,000 deltas   20k chars    0.74 ms    (0.37 ms per 1k deltas)
    #     8,000 deltas   80k chars    9.29 ms    (1.16 ms per 1k deltas)
    #    20,000 deltas  200k chars   55.13 ms    (2.76 ms per 1k deltas)
    #
    # The per-1k cost rising with N is the quadratic term; the local-variable
    # equivalent stays flat at ~0.09 ms per 1k. A list append plus a join on
    # read makes the same work linear.

    @property
    def _live_buf(self) -> str:
        """Streamed answer prose (rope; see _rope_join)."""
        return self._rope_join(self._live_buf_parts)

    @_live_buf.setter
    def _live_buf(self, value: str) -> None:
        self._live_buf_parts = [value] if value else []

    @property
    def _reasoning_buf(self) -> str:
        """Streamed reasoning trace (rope; see _rope_join)."""
        return self._rope_join(self._reasoning_buf_parts)

    @_reasoning_buf.setter
    def _reasoning_buf(self, value: str) -> None:
        self._reasoning_buf_parts = [value] if value else []

    def _flush_stream(self) -> None:
        """Coalescing-timer callback: repaint, then clear the pending latch."""
        self._stream_flush_scheduled = False
        self._paint_stream()

    def _paint_stream(self) -> None:
        """Repaint the in-progress stream/reasoning widgets from their buffers.

        Updates both live widgets in one pass and scrolls once. Called by
        :meth:`_flush_stream` on a coalescing 100ms timer, so a fast token stream
        triggers ~10 repaints/sec instead of one per token, and called directly
        at the start of a stream so the first fragment appears immediately.
        """
        painted = False
        if self._stream_msg is not None:
            # Tail only: repainting the whole buffer every 100ms is quadratic in
            # the answer length, so a model that never stops talking pegs the UI
            # and takes the app (and the terminal state) down with it. The full
            # text is committed as markdown on AssistantMessage; the viewport is
            # pinned to the end anyway.
            self._stream_msg.update_body(
                Text(self._rope_tail(self._live_buf_parts, _LIVE_PREVIEW_CHARS))
            )
            painted = True
        if self._reason_msg is not None:
            self._reason_msg.update_body(
                Text(self._rope_tail(self._reasoning_buf_parts, 2000), style="dim italic")
            )
            painted = True
        if painted:
            # Automatic scroll: a user who scrolled up to read must not be
            # dragged back to the bottom by the next ~100ms repaint.
            self._scroll_end(force=False)

    def _schedule_stream_flush(self) -> None:
        """Ensure a flush happens soon, coalescing bursts of deltas into one."""
        if self._stream_flush_scheduled:
            return
        self._stream_flush_scheduled = True
        self._set_pane_timer(0.1, self._flush_stream, "_stream_flush_scheduled")

    def _set_pane_timer(self, delay, callback, latch) -> None:
        """A deferred repaint belongs to the pane that scheduled it."""
        pane = getattr(self, "_active_pane", None)

        def flush():
            if pane is None or pane is self._active_pane:
                callback()
            else:
                pane.state[latch] = False

        self.set_timer(delay, flush)

    def _theme_color(self, name: str, fallback: str) -> str:
        """Resolve a theme variable to a hex string for a Rich ``Text`` style.

        Rich styles are built as plain strings, so a Textual ``$variable``
        cannot be used directly; this reads the active theme instead. Falls
        back to *fallback* if the theme (or the variable) is unavailable.
        """
        raw = None
        try:
            raw = getattr(self.app.current_theme, name, None)
        except Exception:  # noqa: BLE001 — never break rendering on a theme read
            raw = None
        if not raw:
            try:
                raw = self.app.theme_variables.get(name)
            except Exception:  # noqa: BLE001
                raw = None
        raw = (raw or fallback).strip()
        return raw.removeprefix("ansi_")

    def _render_todos(
        self, todos: list, agent_name: str | None, *, collapsed: bool = False
    ) -> Text:
        """Native todo list: status glyphs + content (no legacy panel).

        The header carries the done/total count and a collapse affordance,
        because when collapsed it is the only row on screen.
        """
        glyphs = {
            "completed": ("☑", "green"),
            "in_progress": ("▶", "yellow"),
            "pending": ("☐", "dim"),
        }
        items = todos or []
        done = sum(1 for td in items if isinstance(td, dict) and td.get("status") == "completed")
        t = Text()
        name = f"{agent_name} · Todos" if agent_name else "Todos"
        caret = "▸" if collapsed else "▾"
        # The "Todos" word carries the theme's accent color (the dock itself has
        # no accent bar), so the header still reads as themed and recolors with
        # /theme.
        t.append(f"{caret} ", style="bold")
        t.append(name, style=f"bold {self._theme_color('accent', '#bb9af7')}")
        t.append(" ", style="bold")
        t.append(
            f"{done}/{len(items)}",
            style="green" if items and done == len(items) else "dim",
        )
        t.append("  click to collapse" if not collapsed else "  click to expand", style="dim")
        t.append("\n")
        if collapsed:
            return t
        for td in todos or []:
            if isinstance(td, dict):
                content = td.get("content", "")
                status = td.get("status", "pending")
            else:
                content, status = str(td), "pending"
            glyph, color = glyphs.get(status, ("☐", "dim"))
            t.append(f"  {glyph} ", style=color)
            t.append(
                f"{content}\n",
                style="strike dim" if status == "completed" else "",
            )
        return t

    def _paint_todos(self, todos: list | None, agent_name: str | None = None) -> None:
        """Repaint the docked checklist, or hide it when there is nothing to show.

        The single place the dock is written. Called on TodoUpdate, on session
        switch (the widget is app-global but ``_todos`` is per-pane, so a switch
        must repaint or pane A's list would sit under pane B), and on resume.

        A fully-completed list is dismissed automatically: the checklist has
        served its purpose and should not keep costing rows above the prompt.
        """
        try:
            dock = self._w("#todo-dock", Static)
            scroll = self._w("#todo-scroll", VerticalScroll)
        except NoMatches:
            return
        items = todos or []
        # Everything checked off -> dismiss. Nothing to track any more, and a
        # stale all-green list is pure noise above the input.
        if not items or all(
            isinstance(td, dict) and td.get("status") == "completed" for td in items
        ):
            dock.remove_class("active")
            scroll.remove_class("active")
            scroll.scroll_home(animate=False)
            _paint(dock, "")
            return
        collapsed = getattr(self, "_todos_collapsed", False)
        # rstrip: every row ends in a newline, which would leave a blank line
        # inside a dock sized to its content.
        text = self._render_todos(items, agent_name, collapsed=collapsed)
        text.rstrip()
        _paint(dock, text)
        dock.set_class(collapsed, "collapsed")
        dock.add_class("active")
        scroll.set_class(collapsed, "collapsed")
        scroll.add_class("active")
        if collapsed:
            scroll.scroll_home(animate=False)

    def action_toggle_todos(self) -> None:
        """Collapse/expand the todo checklist (click the dock, or alt+t)."""
        self._todos_collapsed = not getattr(self, "_todos_collapsed", False)
        self._paint_todos(getattr(self, "_todos", None), getattr(self, "_todos_agent", None))

    # ── dynamic subagents (persistent panel) ────────────────────────────────

    def _ingest_subagent_task(self, task: ev.SubagentTask) -> None:
        """Upsert one dispatch row, then repaint.

        A dispatch arrives twice: once when it is launched (type, label, start
        time) and once when it finishes (status, duration, error). Neither event
        carries all the fields, so the later one inherits what it does not have
        rather than blanking the row.
        """
        existing = self._subagent_rows.get(task.task_id)
        if existing is None and self._subagents_stale:
            # A new turn drops finished history, but background tasks can
            # still be running independently of the previous foreground turn.
            self._subagent_rows = {
                key: row for key, row in self._subagent_rows.items() if row.status == "running"
            }
            phases = {row.phase_id for row in self._subagent_rows.values()}
            self._subagent_phase_order = [p for p in self._subagent_phase_order if p in phases]
            self._subagent_collapsed_phases.intersection_update(phases)
            self._subagent_previews = {
                key: log
                for key, log in self._subagent_previews.items()
                if key in self._subagent_rows
            }
            self._subagents_stale = False
        if existing is not None:
            for field in (
                "phase_id",
                "subagent_type",
                "label",
                "description",
                "started_at",
                "model",
            ):
                if not getattr(task, field, None):
                    setattr(task, field, getattr(existing, field, None))
        if task.model is None:
            # Resolved by role, not blanket-inherited: a subagent or async row may
            # run on a model of its own now. Falls back to the session model when
            # the role is unset, which is what this used to always show.
            from novacode_cli.config.role_models import panel_row_model

            task.model = panel_row_model(task.phase_kind, self.model_name)
        if task.phase_id and task.phase_id not in self._subagent_phase_order:
            self._subagent_phase_order.append(task.phase_id)
        self._subagent_rows[task.task_id] = task
        # Completed and stopped rows disappear from the live panel.
        self._subagents_collapsed = not any(
            row.status == "running" for row in self._subagent_rows.values()
        )
        self._schedule_subagents_paint()

    def _subagent_task_list(self) -> list[Any]:
        """Active rows in arrival order, capped for a runaway fan-out."""
        rows = [row for row in self._subagent_rows.values() if row.status == "running"]
        if len(rows) > subagent_tasks.MAX_ROWS:
            rows = rows[-subagent_tasks.MAX_ROWS :]
        return rows

    def _schedule_subagents_paint(self) -> None:
        """Coalesce repaints: a fan-out delivers its start events in a burst.

        Gated on a deadline, not on the timer handle: the handle can only be
        cleared by the callback, so a one-shot timer that never runs would block
        every later repaint for the rest of the session. A deadline expires, so
        the worst case is one skipped frame.
        """
        now = time.monotonic()
        if now < self._subagents_paint_due:
            return
        self._subagents_paint_due = now + 0.1
        self._subagents_paint_timer = self.set_timer(0.1, self._paint_subagents)

    def _paint_subagents(self) -> None:
        """The single place the panel is written."""
        self._subagents_paint_timer = None
        self._subagents_paint_due = 0.0
        try:
            dock = self._w("#subagents-dock", SubagentsDock)
            title = self._w("#subagents-title", Static)
            body = self._w("#subagents-body", Static)
        except NoMatches:
            return

        rows = self._subagent_task_list()
        dock.set_class(self._subagents_collapsed, "collapsed")
        if not rows:
            dock.remove_class("active")
            _paint(title, "")
            _paint(body, "")
            self._subagents_rows_by_line = []
            self._stop_subagents_tick()
            return

        now = time.time()
        _paint(title, subagent_tasks.panel_title(rows, collapsed=self._subagents_collapsed))
        rendered = subagent_tasks.panel_body(
            rows,
            width=self._subagents_width(dock),
            now=now,
            phase_order=self._subagent_phase_order,
            collapsed_phases=self._subagent_collapsed_phases,
        )
        _paint(body, Text("\n").join(rendered.lines) if rendered.lines else "")
        self._subagents_rows_by_line = [
            (rendered.row_phases[index], rendered.row_tasks[index])
            for index in range(len(rendered.lines))
        ]
        self._subagents_task_column = rendered.task_column
        dock.add_class("active")
        # Only a running row's TIME moves, so the 1s repaint is only worth having
        # while something runs.
        if any(row.status == "running" for row in rows):
            self._start_subagents_tick()
        else:
            self._stop_subagents_tick()

    def _subagents_width(self, dock: Widget) -> int:
        """Cells available to a panel line.

        From the dock's own size minus its horizontal padding (``0 2`` each
        side): ``content_size`` does not subtract padding, and rendering at the
        un-padded width made every row long enough to wrap its TIME column, which
        is the one thing a column layout must never do.
        """
        try:
            width = int(dock.size.width) - 4
        except Exception:  # noqa: BLE001 — not laid out yet
            width = 0
        if width <= 0:
            try:
                width = int(self.size.width or 0) - 6
            except Exception:  # noqa: BLE001
                width = 0
        return max(40, width)

    def _start_subagents_tick(self) -> None:
        if self._subagents_tick is None:
            self._subagents_tick = self.set_interval(1.0, self._paint_subagents)

    def _stop_subagents_tick(self) -> None:
        if self._subagents_tick is not None:
            with suppress(Exception):
                self._subagents_tick.stop()
            self._subagents_tick = None

    def _reset_subagents(self) -> None:
        """Drop the last run's rows, so a new turn starts from an empty panel."""
        self._stop_subagents_tick()
        if self._subagents_paint_timer is not None:
            self._subagents_paint_timer.stop()
            self._subagents_paint_timer = None
        self._subagents_paint_due = 0.0
        self._subagent_rows.clear()
        self._subagent_phase_order.clear()
        self._subagent_collapsed_phases.clear()
        self._subagent_previews.clear()
        self._subagents_collapsed = False
        self._subagents_stale = False
        self._subagents_rows_by_line = []

    def _stop_foreground_subagents(self) -> None:
        """Freeze unfinished sync rows when their owning turn ends."""
        now = time.time()
        for row in self._subagent_rows.values():
            if row.status == "running" and row.phase_kind != "async":
                row.duration_ms = int(subagent_tasks.elapsed_for(row, now) * 1000)
                row.status = "stopped"
        self._subagents_stale = True
        self._paint_subagents()

    def _ingest_async_tasks(self, tasks: object) -> None:
        """Show remote async subagents in the same panel.

        They refresh on the watcher's existing cadence (it polls the runs every
        few seconds), so these rows are near-live rather than live, and they carry
        no duration. Their model belongs to the server rather than to this session,
        so the row names the stored async role, or the server's own default.
        """
        if not isinstance(tasks, dict):
            return
        now = time.time()
        changed = False
        for entry in tasks.values():
            row = subagent_tasks.task_from_async_entry(entry, now=now, model=None)
            if row is None:
                continue
            # The server decides this one's model, so naming the async role (or
            # the server default) is the honest label. It was blanked as unknown,
            # which was right only while a local model was all a row could mean.
            from novacode_cli.config.role_models import panel_row_model

            row.model = panel_row_model("async", self.model_name)
            self._subagent_rows[row.task_id] = row
            if row.phase_id and row.phase_id not in self._subagent_phase_order:
                self._subagent_phase_order.append(row.phase_id)
            changed = True
        if changed:
            self._subagents_collapsed = not any(
                row.status == "running" for row in self._subagent_rows.values()
            )
            self._schedule_subagents_paint()

    def _open_subagent_preview(self, task_id: str) -> None:
        """Show what one row is doing, updating while it runs."""
        opened = self._subagent_rows.get(task_id)
        if opened is None:
            return

        def snapshot() -> tuple[Text, list]:
            row = self._subagent_rows.get(task_id, opened)
            title = Text()
            title.append(f"{subagent_tasks.status_glyph(row.status)} ", style="cyan")
            title.append(row.subagent_type or "subagent", style="bold")
            elapsed = subagent_tasks.format_elapsed(subagent_tasks.elapsed_for(row))
            title.append(f"  {row.status} · {elapsed} · {row.model or '—'}", style="dim")
            if row.phase_kind != "async" and (row.description or row.label):
                title.append(f"\n{row.description or row.label}", style="dim")
            return title, self._subagent_previews.get(task_id) or []

        screen = SubagentPreviewScreen(snapshot)
        self.push_screen(screen)
        if opened.phase_kind == "async":
            self._follow_async_preview(task_id, screen)

    @work(exclusive=True, group="subagent-preview")
    async def _follow_async_preview(self, task_id: str, screen: SubagentPreviewScreen) -> None:
        """Poll a remote agent's thread while its preview is open.

        An async agent runs on the agent server, so nothing it does reaches this
        process's stream: its messages are read from the thread's state instead.
        That is checkpointed per step, so this is step-by-step rather than
        token-by-token.
        """
        from langgraph_sdk import get_client

        from novacode_cli.agents.default_subagents.async_subagents import retrieve_async_subagents

        row = self._subagent_rows.get(task_id)
        if row is None:
            return
        spec = next((s for s in retrieve_async_subagents() if s["name"] == row.subagent_type), None)
        if spec is None:
            self._subagent_previews[task_id] = [("error", "The agent server is not available.")]
            return
        client = get_client(url=spec.get("url"), headers=spec.get("headers") or {})
        thread_id = row.description or task_id.removeprefix("async:")
        while screen in self.screen_stack:
            try:
                state = await client.threads.get_state(thread_id)
                messages = (state.get("values") or {}).get("messages")
                self._subagent_previews[task_id] = subagent_tasks.remote_preview(messages)
            except Exception as exc:  # noqa: BLE001 — a preview must never break the session
                self._subagent_previews[task_id] = [
                    ("error", f"Could not read the agent's thread: {exc}")
                ]
            if self._subagent_rows.get(task_id, row).status != "running":
                return
            await asyncio.sleep(2.0)

    def action_toggle_subagents(self) -> None:
        """Collapse/expand the subagents panel (click the dock, or alt+s)."""
        self._subagents_collapsed = not self._subagents_collapsed
        self._paint_subagents()

    def _on_subagents_click(self, y: int, x: int | None = None) -> None:
        """A click on a task previews it, on a phase folds it; elsewhere folds the panel.

        ``y`` is dock-relative, so line 0 is the header and the table starts one
        line below it. ``x`` is body-relative and says which pane a line was
        clicked in, since a phase and a task share a line; without it the phase
        wins.
        """
        row = y - 1
        if 0 <= row < len(self._subagents_rows_by_line):
            phase_id, task_id = self._subagents_rows_by_line[row]
            if task_id and x is not None and x >= self._subagents_task_column:
                self._open_subagent_preview(task_id)
                return
            if phase_id and (x is None or x < self._subagents_task_column):
                if phase_id in self._subagent_collapsed_phases:
                    self._subagent_collapsed_phases.discard(phase_id)
                else:
                    self._subagent_collapsed_phases.add(phase_id)
                self._paint_subagents()
                return
        self.action_toggle_subagents()

    def _pop_tool(self, call_id: str | None) -> "tuple[Collapsible, Static, str] | None":
        """Find (and stop tracking) the tool component for a result."""
        entry = None
        if call_id and call_id in self._tool_components:
            entry = self._tool_components.pop(call_id)
        elif self._last_tool is not None:
            entry = self._last_tool
        if entry is not None and entry is self._last_tool:
            self._last_tool = None
        return entry

    @staticmethod
    def _render_diff_text(diff: str, max_lines: int = 500, path: str | None = None) -> Text:
        """Render a unified diff natively with +/- coloring (no legacy capture).

        When ``path`` resolves to a known language, syntax token colours are
        overlaid on the diff-marker colour (foreground only), so added/removed
        lines stay visually distinct.
        """
        from novacode_cli.ui.diff_highlight import highlight_line, lexer_for_path

        lexer = lexer_for_path(path)
        t = Text()
        lines = diff.splitlines()
        for line in lines[:max_lines]:
            if line.startswith(("+++", "---")):
                t.append(line + "\n", style="dim")
            elif line.startswith("@@"):
                t.append(line + "\n", style="cyan")
            elif line.startswith(("+", "-")):
                # Use an explicit background so the add/remove signal is a
                # filled block (matching the Rich path's "white on dark_green"
                # / "white on dark_red"), not just coloured text. Syntax token
                # colours overlay the foreground only.
                bg = "dark_green" if line.startswith("+") else "dark_red"
                base = f"white on {bg}"
                marker, code = line[0], line[1:]
                t.append(marker, style=base)
                for text, style in highlight_line(code, lexer):
                    t.append(text, style=f"{style} on {bg}" if style else base)
                t.append("\n")
            else:
                for text, style in highlight_line(line, lexer):
                    t.append(text, style=style or "dim")
                t.append("\n")
        if len(lines) > max_lines:
            t.append(f"… {len(lines) - max_lines} more lines\n", style="dim italic")
        return t

    def _fileop_body(self, rec, full_output: str) -> Text:
        """Native body for a file-op component: diff for writes/edits, content for reads."""
        diff = getattr(rec, "diff", None) if rec is not None else None
        path = getattr(rec, "display_path", None) if rec is not None else None
        if diff:
            return self._render_diff_text(diff, path=path)
        after = getattr(rec, "after_content", None) if rec is not None else None
        if after:  # write with no diff — show the new content as additions
            return self._render_diff_text(
                "\n".join("+" + ln for ln in after.splitlines()), path=path
            )
        out = (
            full_output
            or (getattr(rec, "read_output", None) if rec is not None else None)
            or "(no output)"
        )
        if len(out) > 6000:
            out = out[:6000] + "\n… (truncated)"
        return Text(out)

    def _fileop_summary(self, rec) -> str:
        """A concise '+A / -D' (or 'Read N lines') summary for a file-op title."""
        if rec is None:
            return ""
        tn = getattr(rec, "tool_name", "")
        m = getattr(rec, "metrics", None)
        added = getattr(m, "lines_added", 0) or 0
        removed = getattr(m, "lines_removed", 0) or 0
        if tn == "read_file":
            # Reads populate `lines_read` (file_ops.py), never `lines_written`;
            # reading the wrong field made every read summarize as bare "Read".
            n = getattr(m, "lines_read", 0) or 0
            return f"Read {n} lines" if n else "Read"
        return f"+{added} / -{removed}"

    # -- condensed tool group -------------------------------------------------

    @staticmethod
    def _oneline(s: str, limit: int = 110) -> str:
        """Collapse whitespace/newlines and truncate to a single short line."""
        s = " ".join((s or "").split())
        return s if len(s) <= limit else s[: limit - 1] + "…"

    async def _ensure_tool_group(self) -> None:
        """Create + mount the condensed tool-group panel if one isn't open."""
        if self._tool_group is not None:
            return
        self._tool_group_entries = []
        self._tool_group_lines = {}
        self._tool_group_last_idx = None
        self._tool_group_log_lines = 0
        body = Vertical(classes="toolbody")
        comp = Collapsible(body, title="● tool calls", collapsed=True)
        comp.add_class("tool")
        animate_entrance(comp, "zoom")
        self._tool_group = comp
        self._tool_group_body = body
        # Mount directly (not via _mount) so we don't immediately close the group
        # we're creating.
        await self._transcript().mount(comp)
        await body.mount(Static("", id="tool-group-list"))
        await body.mount(
            OutputLog(
                id="tool-group-log",
                classes="terminal-log",
                highlight=True,
                markup=True,
                max_lines=_LOG_MAX_LINES,
            )
        )
        hint = Static(
            Text("Ctrl+B · run in background", style="dim"), id="tool-group-background-hint"
        )
        hint.display = False
        await body.mount(hint)
        self._prune_transcript()
        self._scroll_end()

    def _close_tool_group(self) -> None:
        """Detach the current tool group so the next burst starts fresh."""
        # Paint any coalesced state BEFORE detaching: a pending timer would
        # otherwise fire after _tool_group is None and drop the final result,
        # leaving the last call showing as still-running.
        if self._tool_group_refresh_scheduled:
            self._tool_group_refresh_scheduled = False
            self._refresh_tool_group(running=self._tool_group_running)
        self._tool_group_running = None
        if self._tool_group_body is not None:
            with contextlib.suppress(NoMatches):
                self._tool_group_body.query_one(
                    "#tool-group-background-hint", Static
                ).display = False
        self._tool_group = None
        self._tool_group_body = None
        self._tool_group_entries = []
        self._tool_group_lines = {}
        self._tool_group_last_idx = None
        self._tool_group_log_lines = 0

    def _render_tool_line(self, entry: dict) -> Text:
        """One compact line for a single tool call in the group body.

        The status mark is a bullet coloured from the active theme rather than an
        emoji: ``⏳``/``✓``/``✗`` are painted by the terminal's own font, so
        ``/theme`` could not move them and they sat at a different weight and
        baseline from the rest of the line. A ``•`` in the theme's success/error/
        warning colour reads as one piece with the text around it.
        """
        pal = self._palette
        err = entry["error"]
        mark = entry["mark"]
        if err:
            mark_style = f"bold {pal.tool_fail}"
        elif mark == "done":
            mark_style = f"bold {pal.tool_ok}"
        else:
            mark_style = f"bold {pal.tool_pending}"
        body_style = pal.tool_fail if err else pal.dim
        t = Text()
        t.append("• ", style=mark_style)
        t.append(entry["base"], style=body_style)
        if entry["detail"]:
            t.append(f"  — {entry['detail']}", style=body_style)
        return t

    def _render_error_detail(self, detail: str) -> Text:
        """A failed call's full output, indented under its line.

        Rendered as a bordered, indented block rather than a nested
        ``Collapsible``: the group body is a single ``Static`` holding one
        ``Text``, so a real widget cannot be mounted inside it without rebuilding
        the whole panel as a container. The block is capped so one enormous
        traceback cannot push the rest of the group off screen, and the cap is
        stated in the text rather than applied silently.
        """
        pal = self._palette
        lines = detail.splitlines() or [detail]
        cap = _TOOL_ERROR_MAX_LINES
        shown = lines[:cap]
        out = Text()
        for line in shown:
            out.append("    │ ", style=pal.faint)
            out.append(line, style=pal.tool_fail)
            out.append("\n")
        if len(lines) > cap:
            out.append("    │ ", style=pal.faint)
            out.append(f"… {len(lines) - cap} more lines", style=pal.dim)
            out.append("\n")
        # Drop the trailing newline; the caller adds its own separator.
        if out.plain.endswith("\n"):
            out = out[:-1]
        return out

    def _schedule_tool_group_refresh(self, *, running: str | None = None) -> None:
        """Coalesce a burst of tool events into one repaint (~100ms), like
        :meth:`_schedule_stream_flush` does for token deltas.

        A tool-heavy turn fires two events per call (start + result), each of
        which repainted the group immediately. Ten quick calls meant twenty
        repaints of the same widget within a few hundred ms, and a repaint at
        120 entries costs ~4 ms. Deferring collapses that to one paint per
        window while the entries themselves stay updated synchronously, so no
        state is lost — only redundant paints.
        """
        # Keep the most recent running label: the last event in the window is
        # the one whose state the paint should show.
        self._tool_group_running = running
        if self._tool_group_refresh_scheduled:
            return
        self._tool_group_refresh_scheduled = True
        self._set_pane_timer(0.1, self._flush_tool_group_refresh, "_tool_group_refresh_scheduled")

    def _flush_tool_group_refresh(self) -> None:
        """Paint the coalesced tool-group state (see _schedule_tool_group_refresh)."""
        self._tool_group_refresh_scheduled = False
        self._refresh_tool_group(running=self._tool_group_running)

    def _refresh_tool_group(self, *, running: str | None = None) -> None:
        """Repaint the group body + title from the current entries.

        Entries are grouped under a category heading (``Explored``, ``Edited``,
        ``Ran``, …) with a count, so a long run of tools reads as a few labelled
        sections rather than one flat list. Categories keep the order they first
        appear in, so the panel mirrors the order the agent actually worked.

        Each line is rendered once and cached on its entry: this runs on EVERY
        tool call and every tool result, and re-rendering all ~100 lines each
        time made tool events O(n) — measured at 4.4 ms per call at 20 calls
        rising to 10.1 ms at 120, the dominant per-event cost on a tool-heavy
        turn. Only the entry that actually changed is re-rendered; the cache is
        dropped by the two mutators (_add_tool_group_call /
        _mark_tool_group_result), so it cannot go stale.
        """
        if self._tool_group is None or self._tool_group_body is None:
            return
        entries = self._tool_group_entries[-100:]

        # Bucket by category, preserving first-seen order.
        sections: dict[str, list[dict]] = {}
        for entry in entries:
            sections.setdefault(entry.get("category") or _OTHER_CATEGORY, []).append(entry)

        body = Text()
        heading_style = f"bold {self._palette.tool_heading}"
        first_section = True
        for cat, items in sections.items():
            if not first_section:
                body.append("\n")
            first_section = False
            # Heading: "→ Explored — 3 reads"
            body.append(f"{_category_glyph(cat)} ", style=heading_style)
            body.append(cat, style=heading_style)
            body.append(f" — {len(items)} {self._category_noun(cat, len(items))}\n", style="dim")
            for entry in items:
                line = entry.get("_line")
                if line is None:
                    line = self._render_tool_line(entry)
                    entry["_line"] = line
                body.append_text(line)
                body.append("\n")
                # A failed call's full output, indented under its line. The
                # one-line detail is a 110-char summary; a traceback is what the
                # user actually needs, and truncating it away was the reason
                # failures were hard to diagnose from the transcript.
                detail = entry.get("error_detail")
                if detail:
                    body.append_text(self._render_error_detail(detail))
                    body.append("\n")
        # Drop the trailing newline from the last line.
        if body.plain.endswith("\n"):
            body = body[:-1]

        try:
            _paint(self._tool_group_body.query_one("#tool-group-list", Static), body)
        except Exception:
            pass
        n = len(self._tool_group_entries)
        self._tool_group.title = motion.tool_group_title(
            n, running, self._spinner_frame, self._palette
        )

    @staticmethod
    def _category_noun(cat: str, count: int) -> str:
        """A pluralised noun for a category heading (``3 reads``, ``1 edit``)."""
        nouns = {
            "Explored": ("read", "reads"),
            "Edited": ("edit", "edits"),
            "Ran": ("command", "commands"),
            "Delegated": ("task", "tasks"),
            "Planned": ("step", "steps"),
            "Remembered": ("memory", "memories"),
            "Produced": ("artifact", "artifacts"),
            "Tasks": ("check", "checks"),
            _OTHER_CATEGORY: ("call", "calls"),
        }
        singular, plural = nouns.get(cat, ("call", "calls"))
        return singular if count == 1 else plural

    def _add_tool_group_call(self, call_id: str | None, base: str, name: str) -> None:
        """Append a 'running' line for a new tool call."""
        entry = {
            "base": self._oneline(base),
            "mark": "running",
            "detail": "",
            "error": False,
            "category": _tool_category(name),
            "backgroundable": name in {"shell", "bash", "execute", "execute_bash", "run_command"},
        }
        idx = len(self._tool_group_entries)
        self._tool_group_entries.append(entry)
        if call_id:
            self._tool_group_lines[call_id] = idx
        self._tool_group_last_idx = idx

        # If running a shell command execution, activate and clear the live log widget
        if (
            name
            in {
                "shell",
                "bash",
                "execute",
                "execute_bash",
                "run_command",
                "run_tests",
                "start_dev_server",
            }
            and self._tool_group is not None
        ):
            # Guard against a concurrently-closed group: _ensure_tool_group sets
            # self._tool_group before its awaits complete, so _close_tool_group
            # (a turn boundary, a queued non-tool event) can null it while this
            # path still runs. Mirrors the guard in _mark_tool_group_result.
            self._tool_group.collapsed = False
            try:
                log_widget = self._tool_group_body.query_one("#tool-group-log", OutputLog)
                log_widget.clear()
                log_widget.add_class("active")
                log_widget.write(Text(f"$ {base}\n"))
                self._tool_group_log_lines = 1 + base.count("\n")
                log_widget.styles.height = min(max(self._tool_group_log_lines + 2, 5), 8)
            except Exception:
                pass

        self._schedule_tool_group_refresh(running=name)
        self._refresh_shell_hint()

    def _refresh_shell_hint(self) -> None:
        """Offer the shortcut only while an execution tool remains in flight."""
        if self._tool_group_body is None:
            return
        with contextlib.suppress(NoMatches):
            hint = self._tool_group_body.query_one("#tool-group-background-hint", Static)
            hint.display = any(
                entry.get("backgroundable") and entry.get("mark") == "running"
                for entry in self._tool_group_entries
            )

    def _trim_tool_group_history(self) -> None:
        """Bound completed tool rows while preserving any still-running calls."""
        entries = self._tool_group_entries
        if len(entries) <= 200:
            return
        keep_from = len(entries) - 200
        kept_indices = [
            index
            for index, entry in enumerate(entries)
            if index >= keep_from or entry.get("mark") == "running"
        ]
        if len(kept_indices) == len(entries):
            return
        remap = {old: new for new, old in enumerate(kept_indices)}
        self._tool_group_entries = [entries[index] for index in kept_indices]
        self._tool_group_lines = {
            call_id: remap[index]
            for call_id, index in self._tool_group_lines.items()
            if index in remap
        }
        if self._tool_group_last_idx is not None:
            self._tool_group_last_idx = remap.get(self._tool_group_last_idx)

    def _mark_tool_group_result(self, call_id: str | None, *, is_error: bool, detail: str) -> None:
        """Finalize the matching tool line with its status + a short result."""
        idx: int | None = None
        if call_id is not None and call_id in self._tool_group_lines:
            idx = self._tool_group_lines[call_id]
        elif self._tool_group_last_idx is not None:
            idx = self._tool_group_last_idx
        if idx is None or idx >= len(self._tool_group_entries):
            # No open group line (group already closed) — compact fallback line.
            if detail:
                self._log(
                    Text(
                        f"  ⎿  {self._oneline(detail)}",
                        style=self._palette.tool_fail if is_error else self._palette.dim,
                    )
                )
            return
        entry = self._tool_group_entries[idx]
        entry["mark"] = "failed" if is_error else "done"
        entry["error"] = is_error
        entry["detail"] = self._oneline(detail)
        entry["_line"] = None  # fields changed → drop the cached render
        self._refresh_shell_hint()
        # Surface failures: pop the group open so the error isn't hidden — and
        # paint NOW rather than on the coalescing timer, or the group would
        # expand to show content that is still up to 100 ms stale.
        if is_error and self._tool_group is not None:
            self._tool_group.collapsed = False
            self._tool_group_refresh_scheduled = False
            self._refresh_tool_group()
        else:
            self._schedule_tool_group_refresh()

    def _set_tool_error_detail(self, call_id: str | None, full_output: str) -> None:
        """Attach a failed call's full output so the group can expand it.

        The one-line ``detail`` is a 110-char summary; a traceback or a compiler
        error is far longer and is exactly what the user needs to read. Storing
        the full text on the entry lets :meth:`_refresh_tool_group` render it
        under a nested ``Collapsible`` instead of truncating it away.
        """
        idx: int | None = None
        if call_id is not None and call_id in self._tool_group_lines:
            idx = self._tool_group_lines[call_id]
        elif self._tool_group_last_idx is not None:
            idx = self._tool_group_last_idx
        if idx is None or idx >= len(self._tool_group_entries):
            return
        text = (full_output or "").strip()
        if not text:
            return
        if len(text) > _TOOL_ERROR_MAX_CHARS:
            text = text[:_TOOL_ERROR_MAX_CHARS] + "\n… (truncated)"
        self._tool_group_entries[idx]["error_detail"] = text

    def _finalize_tool(
        self, call_id: str | None, preview: str, full_output: str, *, is_error: bool
    ) -> None:
        entry = self._pop_tool(call_id)
        if entry is None:
            self._log(
                Text(
                    f"  ⎿  {preview}",
                    style=self._palette.tool_fail if is_error else self._palette.dim,
                )
            )
            return
        comp, body, base = entry
        for hint in comp.query(".background-shell-hint"):
            hint.display = False
        pal = self._palette
        mark = "•"
        comp.title = f"{base}  {mark} {_esc(preview)}"
        out = full_output or "(no output)"
        if len(out) > _TOOL_ERROR_MAX_CHARS:
            out = out[:_TOOL_ERROR_MAX_CHARS] + "\n… (truncated)"
        body_style = pal.tool_fail if is_error else ""
        if isinstance(body, OutputLog):
            body.clear()
            body.write(Text(out, style=body_style))
            body.scroll_end(animate=False)
        else:
            body.update(Text(out, style=body_style))
        # Animate border to settled state. Theme-derived, not the tokyo-night
        # hexes this used to hardcode — those survived /theme.
        final_color = pal.tool_fail if is_error else pal.tool_ok
        try:
            comp.styles.animate("border_left", f"thick {final_color}", duration=0.35)
        except Exception:  # noqa: BLE001
            pass

    async def _handle_subagent(self, e: ev.SubagentActivity) -> None:
        """Render subagent dispatch and completion with collapsible widgets."""
        import time

        cid = e.call_id or ""
        color = e.color or "#bb9af7"

        if e.kind == "dispatched" and cid:
            self._subagent_count += 1
            label = f"⟐ {e.subagent_type or 'subagent'}"
            title = Text.assemble(
                (label, f"bold {color}"),
                (f"  · #{self._subagent_count} dispatched", "dim"),
            )
            # Create a Vertical container as the body
            body = Vertical(classes="toolbody")
            # Start expanded (collapsed=False) so dispatching subagents show live progress
            comp = Collapsible(body, title=title, collapsed=False)  # type: ignore
            comp.add_class("subagent")
            await self._mount(comp)
            animate_entrance(comp, "fade")

            # Mount a Static for status text, a Static for subagent-list, and a OutputLog for the progress log
            status_text = Text(e.detail or "", style="dim") if e.detail else Text("")
            await body.mount(Static(status_text, id="subagent-status"))
            await body.mount(Static("", id="subagent-list"))
            await body.mount(
                OutputLog(
                    id="subagent-log",
                    classes="terminal-log",
                    highlight=True,
                    markup=True,
                    max_lines=_LOG_MAX_LINES,
                )
            )

            # Initialize dynamic height tracking and entry lists
            comp._log_lines = 0
            comp._log_entries = []
            comp._tool_lines = {}
            try:
                log_widget = body.query_one("#subagent-log", OutputLog)
                log_widget.styles.height = 5
            except Exception:
                pass

            self._subagent_widgets[cid] = (
                comp,
                body,
                e.subagent_type or "subagent",
                time.time(),
            )

        elif e.kind == "completed":
            # Try matching by call_id first, then fallback to subagent_type
            entry = None
            matched_cid = cid
            if cid and cid in self._subagent_widgets:
                entry = self._subagent_widgets.pop(cid)
            else:
                # Fallback: find first matching by subagent type
                for key, val in list(self._subagent_widgets.items()):
                    if val[2] == (e.subagent_type or ""):
                        entry = self._subagent_widgets.pop(key)
                        matched_cid = key
                        break

            if entry is not None:
                comp, body, stype, start_time = entry
                elapsed = time.time() - start_time
                dur = (
                    f"{elapsed:.1f}s"
                    if elapsed < 60
                    else f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                )
                icon = e.message or f"{e.subagent_type}"
                count = len(self._subagent_widgets)
                remaining = f" · {count} active" if count > 0 else ""
                comp.title = f"{_esc(str(icon))}  ({dur}){remaining}"
                if e.detail:
                    try:
                        _paint(
                            body.query_one("#subagent-status", Static),
                            Text(e.detail, style="dim"),
                        )
                    except Exception:
                        pass
                else:
                    try:
                        _paint(body.query_one("#subagent-status", Static), Text(""))
                    except Exception:
                        pass
                comp.collapsed = True
                # Clean up tool calls mapping for this subagent
                self._subagent_tool_to_task = {
                    k: v for k, v in self._subagent_tool_to_task.items() if v != matched_cid
                }
                # Subagent completion is already reflected in the digest's task
                # count, so no separate remote message is sent here.
            else:
                # No matching widget — log as a simple line
                dur_part = ""
                if e.detail:
                    dur_part = f" — {e.detail}"
                self._log(Text(f"{e.message}{dur_part}", style=color))

        elif e.kind in ("status", "tool_start", "tool_result") and e.message:
            if e.kind == "tool_start" and e.detail and cid:
                self._subagent_tool_to_task[e.detail] = cid
            elif e.kind == "tool_result" and e.detail:
                self._subagent_tool_to_task.pop(e.detail, None)

            if cid and cid in self._subagent_widgets:
                comp, body, stype, start_time = self._subagent_widgets[cid]
                log_entries = getattr(comp, "_log_entries", [])
                tool_lines = getattr(comp, "_tool_lines", {})

                if e.kind == "tool_start":
                    if e.detail and e.detail in tool_lines:
                        idx = tool_lines[e.detail]
                        entry = log_entries[idx]
                        entry["display"] = e.message
                        entry.pop("_line", None)  # invalidate the cached render
                    else:
                        entry = {
                            "type": "tool",
                            "display": e.message,
                            "mark": "running",
                            "detail": "",
                            "error": False,
                        }
                        idx = len(log_entries)
                        log_entries.append(entry)
                    if e.detail:
                        tool_lines[e.detail] = idx
                    comp._log_entries = log_entries
                    comp._tool_lines = tool_lines
                    self._trim_subagent_log(comp)
                    self._refresh_subagent_list(cid)
                elif e.kind == "tool_result":
                    if e.detail and e.detail in tool_lines:
                        idx = tool_lines[e.detail]
                        entry = log_entries[idx]
                        is_error = e.color == "#f7768e"
                        entry["mark"] = "failed" if is_error else "done"
                        entry["detail"] = e.message
                        entry["error"] = is_error
                        entry.pop("_line", None)  # invalidate the cached render
                        tool_lines.pop(e.detail, None)
                    comp._log_entries = log_entries
                    comp._tool_lines = tool_lines
                    self._trim_subagent_log(comp)
                    self._refresh_subagent_list(cid)
                else:  # status
                    log_entries.append(
                        {
                            "type": "status",
                            "display": e.message,
                        }
                    )
                    comp._log_entries = log_entries
                    self._trim_subagent_log(comp)
                    self._refresh_subagent_list(cid)
            else:
                self._log(Text(f"  ⟐ {e.message}", style=color))

    def _refresh_subagent_list(self, cid: str) -> None:
        """Redraw the subagent Static list based on its current entries.

        Mirrors ``_refresh_tool_group``: this runs on EVERY subagent event, and
        ``comp._log_entries`` grows for the life of the subagent, so re-rendering
        all of them each time made subagent events O(n^2). Each line is rendered
        once and cached on its entry, and only the tail is drawn.
        """
        if cid not in self._subagent_widgets:
            return
        comp, body, stype, start_time = self._subagent_widgets[cid]
        try:
            list_widget = body.query_one("#subagent-list", Static)
            log_entries = getattr(comp, "_log_entries", [])[-_SUBAGENT_LIST_TAIL:]
            lines = []
            for entry in log_entries:
                line = entry.get("_line")
                # The rendered line embeds theme colours, so a /theme switch must
                # invalidate the cache — key it on the active theme name.
                if line is None or entry.get("_line_theme") != self.theme:
                    line = self._render_subagent_line(entry)
                    entry["_line"] = line
                    entry["_line_theme"] = self.theme
                lines.append(line)
            _paint(list_widget, "\n".join(lines))
        except Exception:
            pass

    @staticmethod
    def _trim_subagent_log(comp: Any) -> None:
        """Bound a subagent card's event history while retaining active calls."""
        entries = getattr(comp, "_log_entries", [])
        if len(entries) <= _SUBAGENT_LOG_MAX_ENTRIES:
            return
        keep_from = len(entries) - _SUBAGENT_LOG_MAX_ENTRIES
        kept_indices = [
            index
            for index, entry in enumerate(entries)
            if index >= keep_from or entry.get("mark") == "running"
        ]
        if len(kept_indices) == len(entries):
            return
        remap = {old: new for new, old in enumerate(kept_indices)}
        comp._log_entries = [entries[index] for index in kept_indices]
        comp._tool_lines = {
            call_id: remap[index]
            for call_id, index in getattr(comp, "_tool_lines", {}).items()
            if index in remap
        }

    def _render_subagent_line(self, entry: dict) -> str:
        """Render one subagent log entry to a markup line (cached by the caller).

        Colours come from the active theme rather than the tokyo-night hexes this
        used to hardcode, and the status mark is a geometric glyph rather than
        ``✓``/``✗`` — the emoji are painted by the terminal's own font, so
        ``/theme`` could not move them.
        """
        if entry.get("type") == "status":
            return f"⟐ {entry['display']}"
        pal = self._palette
        mark = entry["mark"]
        display = entry["display"]
        detail = entry["detail"]
        error = entry["error"]
        if error:
            color = pal.tool_fail
        elif mark in ("done", "●"):
            color = pal.tool_ok
        else:
            color = pal.accent
        glyph = "✖" if error else ("●" if mark in ("done", "●") else "◐")

        line = f"[{color}]{glyph}[/{color}] {display}"
        if detail:
            clean_detail = detail
            if clean_detail in ("●", "✖", "done", "failed"):
                clean_detail = ""
            if clean_detail.startswith(("● ", "✖ ")):
                clean_detail = clean_detail[2:]
            if clean_detail:
                line += f" [dim]· {clean_detail}[/dim]"
        return f"⟐ {line}"

    async def _remove_reasoning(self) -> None:
        """Finalize the reasoning trace: collapse it and keep it in the transcript.

        The trace used to be deleted at the end of the turn, so the model's
        thinking vanished the moment the answer arrived. It is now retained as a
        collapsed one-line card the user can expand, which keeps the transcript
        honest about what the model did without letting a long trace dominate it.
        """
        if self._reason_msg is not None:
            try:
                # Commit the full trace (the live view only showed a tail) and
                # fold it to its header.
                self._reason_msg.update_body(Text(self._reasoning_buf, style="dim italic"))
                self._reason_msg.set_collapsed(collapsed=True)
            except Exception:  # noqa: BLE001 — finalizing must never break the turn
                pass
            self._reason_msg = None
        self._reasoning_buf = ""

    #: Cached right-docked status counts (see _refresh_status). Class-level so a
    #: read before the first tail rebuild yields an empty Text, not AttributeError.
    _status_right: Text | None = None
    _status_timer: Timer | None = None

    def _set_status(self, activity: str) -> None:
        self._activity = activity
        self._refresh_status()
        self._schedule_status_tick()

    def _schedule_status_tick(self) -> None:
        """Use the chosen animation rate while focused/busy, 2 Hz otherwise."""
        if self._status_timer is not None:
            self._status_timer.stop()
        if not self.is_mounted:
            self._status_timer = None
            return
        if self._turn_active and self._os_focused:
            delay = 0.2 if self._low_resource_mode else 1 / self._animation_fps
        else:
            delay = 0.5
        self._status_timer = self.set_timer(delay, self._status_tick)

    def _status_tick(self) -> None:
        """Poll background state and schedule the next adaptive refresh."""
        self._tick()
        self._schedule_status_tick()

    def _event_color(self, name: str) -> str:
        """Map a Nova event's semantic colour name to the active theme's palette.

        The learning feature emits events with an ANSI colour name (``"cyan"``,
        ``"green"``, …). Those resolve against the *terminal's* 16-colour palette,
        not the Textual theme, so a ``/theme`` switch left every learning notice
        the same colour while the CSS border around it recoloured. Mapping the
        name here — the one place that has a theme — keeps the two in step.

        Args:
            name: The event's colour name, or any unrecognised string.

        Returns:
            A palette colour, falling back to the theme's accent.
        """
        pal = self._palette
        return {
            "cyan": pal.accent,
            "green": pal.success,
            "yellow": pal.warning,
            "red": pal.error,
            "magenta": pal.agent_label,
            "dim": pal.dim,
        }.get(name, pal.accent)

    def _set_nova_indicator(
        self, text: str, *, style: str = "dim", auto_clear: float | None = None
    ) -> None:
        """Show the Nova learning status (review cycle) inline in the status line.

        The status renders beside the context % (see :meth:`_refresh_status`) so
        it never overlaps the input box. Empty ``text`` clears it.

        Args:
            text: Message to display. Empty string clears the status.
            style: Rich style for the text.
            auto_clear: If set, clear the status after this many seconds.
        """
        # Cancel any pending auto-clear so a new message isn't wiped early.
        if self._nova_indicator_timer is not None:
            try:
                self._nova_indicator_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._nova_indicator_timer = None

        self._nova_status = text or None
        self._nova_status_style = style
        self._refresh_status()

        if text and auto_clear is not None:
            self._nova_indicator_timer = self.set_timer(
                auto_clear, lambda: self._set_nova_indicator("")
            )

    def _start_compaction_indicator(self) -> None:
        """Animate context compaction in the footer without exposing its draft."""
        frames = ("◐", "◓", "◑", "◒")
        self._compaction_indicator_frame = 0

        def animate() -> None:
            frame = frames[self._compaction_indicator_frame % len(frames)]
            self._compaction_indicator_frame += 1
            self._set_nova_indicator(f"{frame} Context is compacting…", style="dim cyan")

        animate()
        timer = getattr(self, "_compaction_indicator_timer", None)
        if timer is not None:
            timer.stop()
        self._compaction_indicator_timer = self.set_interval(0.16, animate)

    def _stop_compaction_indicator(self) -> None:
        """Stop the compaction animation and release the footer status slot."""
        timer = getattr(self, "_compaction_indicator_timer", None)
        if timer is not None:
            timer.stop()
            self._compaction_indicator_timer = None
        if getattr(self, "_nova_status", None) and "Context is compacting" in self._nova_status:
            self._set_nova_indicator("")

    def _ctx_gauge(self, percent: float, pal: FooterPalette | None = None, width: int = 12) -> Text:
        """A two-tone fill meter for *percent* across *width* cells.

        Returns a :class:`rich.text.Text` rather than a bare string because the
        filled cells and the empty track MUST be styled differently. The old
        version returned one string that the caller painted in a single colour,
        so at 6% the roughly ten empty cells were as bright as the filled one
        and the bar read as full while the number beside it said 6%.

        Palette colours are taken from the active theme (see
        :mod:`novacode_cli.tui.palette`) so the meter recolours with ``/theme``.

        Args:
            percent: Context-window fill, 0-100.
            pal: A :class:`~novacode_cli.tui.palette.FooterPalette`, or None to
                resolve the active theme's palette.
            width: Number of cells in the track.

        Returns:
            The meter as a styled ``Text``.
        """
        if pal is None:
            pal = self._palette
        percent = max(0.0, min(100.0, percent))
        color = (
            pal.error
            if percent >= _PCT_CRITICAL
            else (pal.warning if percent >= _PCT_WARN else pal.success)
        )
        filled = percent / 100.0 * width
        full = int(filled)
        rem = round((filled - full) * 8)
        eighths = " ▏▎▍▌▋▊▉█"

        t = Text()
        t.append("▐", style=pal.dim)
        if full == 0 and rem == 0:
            # Never paint an entirely empty track: a single eighth-block stub
            # keeps "0-ish" legible as a small amount rather than a broken bar.
            t.append("▏", style=color)
            t.append("─" * max(0, width - 1), style=pal.faint)
        else:
            t.append("█" * full, style=color)
            if full < width and rem > 0:
                t.append(eighths[min(8, rem)], style=color)
                full += 1
            if full < width:
                t.append("─" * (width - full), style=pal.faint)
        t.append("▌", style=pal.dim)
        return t

    def _refresh_status(self) -> None:
        pal = self._palette
        line = Text()

        # Activity segment — animated spinner + elapsed while live, else a ● dot.
        # Rebuilt every animation frame; the heavier state below is cached.
        if self._turn_active:
            elapsed = time.monotonic() - self._turn_start
            # A single-glyph spinner (so the label never jitters) plus a light
            # band sweeping across the words. The motion says "working" without
            # moving the text, which is the difference between polish and noise.
            line.append(f"{motion.spinner(self._spinner_frame)} ", style=f"bold {pal.accent}")
            line.append_text(motion.shimmer(str(self._activity).upper(), self._spinner_frame, pal))
            line.append(f"  {elapsed:0.1f}s", style=pal.muted)
        else:
            line.append("● ", style=f"bold {pal.success}")
            line.append(str(self._activity).upper(), style=f"bold {pal.success}")

        # Heavy tail (ctx gauge, bridge, notifs) and the right-aligned counts
        # change slowly. Rebuild them at most ~4x/sec so the per-frame spinner
        # update stays cheap; _tick drops the cache to None when a notif/bridge
        # change must show at once.
        now = time.monotonic()
        ttl = 0.25  # rebuild the heavy tail at most ~4x/sec
        last = getattr(self, "_status_tail_ts", 0.0)
        if getattr(self, "_status_tail", None) is None or now - last > ttl:
            self._status_tail, self._status_right = self._build_status_tail()
            self._status_tail_ts = now
        if self._status_tail is not None:
            line.append_text(self._status_tail)

        # layout=False: this bar is `width: 1fr; height: 1`, so no content can
        # change its size. Static.update() defaults to layout=True, and a layout
        # pass re-arranges the WHOLE screen — this call runs up to 60x/sec while a
        # turn is live, so the default cost two full reflows per frame (the
        # freeze log: 1,000 of 1,822 frozen seconds were this reflow).
        try:
            self._w("#prompt-hint-bar", Static).update(line, layout=False)
        except NoMatches:
            pass
        # The counts live in their own right-docked widget. A Rich Text cannot
        # right-align itself, but a Horizontal with a `1fr` left cell and an
        # `auto` right cell does — so the counts sit flush against the edge
        # instead of trailing the status text wherever it happens to end.
        right = self._status_right
        if self._narrow or self._compact:
            right = Text()
        right = right if right is not None else Text()
        # `width: auto`, so a layout IS needed — but only when the width actually
        # changes, and nothing at all when the text is identical (it is rebuilt
        # ~4x/sec and usually comes out the same).
        try:
            _paint(self._w("#status-counts", Static), right)
        except NoMatches:
            pass

    def _build_status_tail(self) -> tuple[Text, Text]:
        """Slow-changing status segments, cached on a short TTL by _refresh_status.

        Returns ``(left, right)``: the left segment follows the activity text and
        the right one is right-docked by the status row's layout. Split out so
        the 20fps spinner refresh doesn't rebuild the ctx gauge, bridge scan,
        notification counts, and skill/file counts every frame.
        """
        pal = self._palette
        line = Text()
        right = Text()

        def _divider() -> None:
            line.append("   ┃   ", style=pal.rail)

        # Context segment — a filling gauge that recolors green→amber→red.
        if self.token_tracker is not None:
            try:
                bd = self.token_tracker.get_breakdown()
            except Exception:  # noqa: BLE001
                bd = None
            if bd is not None:
                p = bd.usage_percentage
                ctx_color = _pct_color(p, pal.success, pal)
                _divider()
                line.append("CTX ", style=pal.dim)
                line.append_text(self._ctx_gauge(p, pal))
                line.append(f"  {p:.0f}%", style=f"bold {ctx_color}")

        if self.token_tracker is not None:
            cache_summary_fn = getattr(self.token_tracker, "cache_summary", None)
            cache_summary = (
                cache_summary_fn(compact=self._narrow or self._compact)
                if callable(cache_summary_fn)
                else None
            )
            if cache_summary:
                _divider()
                hit_rate = getattr(self.token_tracker, "cache_hit_percentage", None)
                cache_color = pal.success if hit_rate else pal.dim
                line.append(cache_summary, style=f"bold {cache_color}")

        # Nova learning status (review cycle).
        if self._nova_status:
            _divider()
            line.append("◆ ", style=pal.accent)
            line.append(self._nova_status, style=self._nova_status_style)

        # Remote bridge indicator — platform abbreviations when a bridge is live.
        try:
            mgr = getattr(self.session_state, "_remote_bridge_manager", None)
            if mgr is not None:
                _active = [
                    b for b in mgr.active_bridges if b.get("status") in ("running", "connecting...")
                ]
                if _active:
                    _divider()
                    line.append("📡 ", style=f"bold {pal.primary}")
                    _labels = []
                    for _b in _active:
                        _plat = str(_b.get("platform", "")).lower()
                        _label = "TG" if _plat == "telegram" else _plat[:3].upper()
                        _status = _b.get("status", "")
                        _style = pal.dim if _status == "connecting..." else f"bold {pal.primary}"
                        _labels.append((_label, _style))
                    for _i, (_lbl, _sty) in enumerate(_labels):
                        if _i:
                            line.append(" · ", style=pal.rail)
                        line.append(_lbl, style=_sty)
        except Exception:  # noqa: BLE001
            pass

        notif = self._unread_count()
        if notif:
            _divider()
            line.append("🔔 ", style=f"bold {pal.warning}")
            line.append(str(notif), style=f"bold {pal.warning}")

        pending = self._pending_approval_count()
        if pending:
            _divider()
            line.append("⚡ ", style=f"bold {pal.warning}")
            line.append(str(pending), style=f"bold {pal.warning}")

        # Right-docked counts. Skills shows the *enabled* set so it reflects
        # /skills toggles, not the full installed list. Dropped on narrow
        # terminals where there is no room for them.
        skill_count = 0 if self._narrow or self._compact else self._cached_enabled_skill_count()
        file_count = 0 if self._narrow or self._compact else self._cached_agent_md_count()

        if file_count:
            right.append(
                f"{file_count} NOVA.md file{'s' if file_count != 1 else ''}", style=pal.muted
            )
        if skill_count:
            if file_count:
                right.append("  ·  ", style=pal.rail)
            right.append(f"{skill_count} skill{'s' if skill_count != 1 else ''}", style=pal.muted)

        return line, right

    def _unread_count(self) -> int:
        """Unread notification count (0 on any error)."""
        try:
            return self.session_state.unread_notification_count()
        except Exception:  # noqa: BLE001
            return 0

    def _pending_approval_count(self) -> int:
        """Pending approval count (0 on any error)."""
        try:
            return self.session_state.pending_approval_count()
        except Exception:  # noqa: BLE001
            return 0

    def _refresh_hint_bar(self) -> None:
        """Populate the hint bar above the input (delegates to _refresh_status)."""
        self._refresh_status()

    def _refresh_info_bar(self) -> None:
        """Refresh the info-bar columns below the input.

        Workspace / sandbox / model / quota update synchronously; the git branch
        is read off-thread (``_refresh_branch_worker``) so a slow repo can't stall
        the UI. Safe to call repeatedly — used at mount and on a refresh timer, so
        a model switch, branch change, or sandbox change shows up live.

        Colours come from the active theme's palette so ``/theme`` recolours the
        footer like every other surface (these were hardcoded tokyo-night hexes,
        which stayed neon under any other theme).
        """
        from novacode_cli.config.config import settings

        pal = self._palette
        self._set_info(
            "#info-workspace", Text(str(settings.get_workspace_root()), style=f"bold {pal.text}")
        )

        sandbox_type = getattr(self.session_state, "_sandbox_type", None)
        if sandbox_type:
            sandbox_text = Text(str(sandbox_type), style=f"bold {pal.warning}")
        else:
            sandbox_text = Text("no sandbox", style=pal.warning)
        self._set_info("#info-sandbox", sandbox_text)

        router_enabled = bool(getattr(self, "_router_mode_enabled", False))
        label, model = _router_model_display(
            router_enabled, self.model_name, getattr(self, "_routed_model_name", None)
        )
        label_color = pal.accent if router_enabled else pal.muted
        self._set_info("#info-model-label", Text(label, style=f"bold {label_color}"))
        self._set_info("#info-model", Text(model, style=f"bold {pal.primary}"))
        self._refresh_quota()
        # The artifacts cell is otherwise only repainted by its registry observer,
        # so a /theme switch left it carrying the previous theme's colour while
        # every cell beside it recoloured.
        self._refresh_artifacts_component()
        self._refresh_branch_worker()

    def _set_info(self, selector: str, renderable: Text) -> None:
        """Update an info-bar Static, ignoring it if not mounted yet."""
        try:
            _paint(self._w(selector, Static), renderable)
        except NoMatches:
            pass

    @work(thread=True, exclusive=True, group="infobar")
    def _refresh_branch_worker(self) -> None:
        """Read the current git branch off the event loop and update the info bar."""
        import subprocess
        from novacode_cli.config.config import settings

        branch = "—"
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                cwd=str(settings.get_workspace_root()),
            )
            if result.returncode == 0:
                branch = result.stdout.strip() or "—"
        except Exception:  # noqa: BLE001
            branch = "—"
        pal = self._palette
        self.call_from_thread(
            self._set_info, "#info-branch", Text(branch, style=f"bold {pal.accent}")
        )

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        """Compact token count: 950 → '950', 12_300 → '12k', 1_240_000 → '1.2M'."""
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}k"
        return str(n)

    def _refresh_quota(self) -> None:
        """Context + session-usage refresh (called from _tick during active turns).

        Shows two numbers, because they answer different questions and move
        independently:

        * **ctx** — how full the context window is right now. Bounded, and it
          DROPS when /compact summarizes the conversation.
        * **used** — cumulative input+output over every turn. Monotonic: it
          tracks total spend, so /compact deliberately leaves it alone (see
          ``TokenTracker.reset``).

        Showing only the cumulative number made /compact look like a no-op, so
        both are rendered side by side.

        The cumulative figure is a COUNT, not a percentage. It used to render as
        ``{pct}% of {budget} budget`` against ``TokenTracker.session_token_budget``
        — a hardcoded 1M that nothing reads, enforces, or lets the user configure.
        Because the count is monotonic, that meter was guaranteed to read past
        100% in any long session (observed at 1087%) and then sat in the error
        colour for the rest of the run, which is a meter reporting its own
        brokenness rather than a limit. A count has no ceiling to exceed.
        """
        parts: list[tuple[str, str]] = []
        pal = self._palette

        # Context-window fill (bounded; drops on compaction).
        try:
            bd = self.token_tracker.get_breakdown() if self.token_tracker else None
        except Exception:  # noqa: BLE001 — a display read must never break the bar
            bd = None
        if bd is not None and getattr(bd, "context_window_size", 0):
            ctx_pct = bd.usage_percentage
            parts.append((f"ctx {ctx_pct:.0f}%", f"bold {_pct_color(ctx_pct, pal.success, pal)}"))

        # Cumulative session usage (monotonic; survives compaction). Rendered as
        # a count, not a percentage of a budget — see the docstring.
        tot = getattr(self.token_tracker, "session_total_tokens", 0)
        if tot:
            parts.append((f"{self._fmt_tokens(tot)} used", f"bold {pal.primary}"))

        if not parts:
            usage_text = Text("—", style=pal.dim)
        else:
            usage_text = Text()
            for i, (label, style) in enumerate(parts):
                if i:
                    usage_text.append(" · ", style="dim")
                usage_text.append(label, style=style)
        try:
            _paint(self._w("#info-quota", Static), usage_text)
        except NoMatches:
            pass

    def _tick(self) -> None:
        refresh = False
        tail_dirty = False
        if self._turn_active and self._os_focused:
            # Wall time keeps motion speed stable when the frame-rate setting
            # changes, and skips missed frames rather than trying to catch up.
            self._spinner_frame = int(time.monotonic() * motion.CLOCK_FPS)
            refresh = True
            # A tool group with a live tool also animates, so re-title it every
            # tick. This is one attribute assignment on one widget (cheap); it
            # matters because a title painted only on tool events would freeze
            # mid-sweep for the whole duration of a slow tool.
            if self._tool_group is not None and self._tool_group_running:
                try:
                    _retitle(
                        self._tool_group,
                        motion.tool_group_title(
                            len(self._tool_group_entries),
                            self._tool_group_running,
                            self._spinner_frame,
                            self._palette,
                        ),
                    )
                except Exception:  # noqa: BLE001 — a title must never break a turn
                    pass
        # Animation FPS must not multiply background-state polling work.
        now = time.monotonic()
        if now - getattr(self, "_last_background_poll", 0.0) < 0.25:
            if refresh:
                self._refresh_status()
            return
        self._last_background_poll = now
        # Surface notifications raised by background tasks within ~250ms.
        cur = self._unread_count()
        if cur != self._last_notif_count:
            self._last_notif_count = cur
            refresh = True
            tail_dirty = True
        # Remote bridge liveness — refresh when the active bridge count changes
        # (bridge connects, disconnects, or watchdog restarts it).
        try:
            _mgr = getattr(self.session_state, "_remote_bridge_manager", None)
            _bridge_count = (
                len(
                    [
                        b
                        for b in _mgr.active_bridges
                        if b.get("status") in ("running", "connecting...")
                    ]
                )
                if _mgr is not None
                else 0
            )
        except Exception:  # noqa: BLE001
            _bridge_count = 0
        if _bridge_count != getattr(self, "_last_bridge_count", -1):
            self._last_bridge_count = _bridge_count
            refresh = True
            tail_dirty = True
        # A notif/bridge change must show at once — drop the throttled tail cache
        # so _refresh_status rebuilds it this frame instead of up to 0.25s later.
        if tail_dirty:
            self._status_tail = None
        if refresh:
            self._refresh_status()

    # -- input ----------------------------------------------------------------
    def _update_mode_badge(self, input_value: str = "") -> None:
        """Show a mode badge and restyle/animate the input for plan/bash modes.

        Bash takes visual precedence over plan when both apply (you can be in
        plan mode and still type a ``!command``). Styling is driven by CSS
        classes (``bash-mode`` / ``plan-mode``) plus a per-mode pulse so each
        mode has a distinct look *and* a distinct animation.
        """
        plan = getattr(self.session_state, "plan_mode_enabled", False)
        bash = input_value.startswith("!")
        goal = getattr(self.session_state, "active_goal", None)
        # Skip the badge/class/pulse work entirely when the mode is unchanged —
        # this runs on every keystroke, so the common case (mode didn't change)
        # must be a cheap no-op.
        if self._last_mode_state == (plan, bash, bool(goal)):
            return
        self._last_mode_state = (plan, bash, bool(goal))
        try:
            badge = self._w("#mode-badge", Static)
            prompt = self._w("#prompt", PromptInput)
        except NoMatches:
            return

        pal = self._palette
        if plan and bash:
            t = Text()
            t.append("  ⏸ PLAN  ", style=f"bold {pal.primary}")
            t.append("$ BASH — runs in chat · !! for the terminal", style=f"bold {pal.accent}")
            _paint(badge, t)
            badge.display = True
        elif plan:
            _paint(
                badge, Text("  ⏸ PLAN MODE — proposing, not editing", style=f"bold {pal.primary}")
            )
            badge.display = True
        elif bash:
            _paint(
                badge,
                Text("  $ BASH — runs in chat · !! for the terminal", style=f"bold {pal.accent}"),
            )
            badge.display = True
        elif goal:
            short = goal if len(goal) <= 60 else goal[:57] + "…"
            _paint(badge, Text(f"  🎯 GOAL — {short}", style=f"bold {pal.warning}"))
            badge.display = True
        else:
            _paint(badge, "")
            badge.display = False

        # Drive the input look from CSS classes (bash wins over plan visually).
        prompt.set_class(bash, "bash-mode")
        prompt.set_class(plan and not bash, "plan-mode")

        # Also style the > prefix chevron and the prompt row to match.
        try:
            prefix = self.query_one("#prompt-prefix", Static)
            prefix.set_class(bash, "bash-mode")
            prefix.set_class(plan and not bash, "plan-mode")
            _paint(prefix, "$ " if bash else "> ")
        except NoMatches:
            pass
        try:
            row = self.query_one("#prompt-row", Horizontal)
            row.set_class(bash, "bash-mode")
            row.set_class(plan and not bash, "plan-mode")
        except NoMatches:
            pass

        # Distinct animation per mode.
        self._set_input_pulse("bash" if bash else ("plan" if plan else None))

    def _set_input_pulse(self, mode: str | None) -> None:
        """Animate the input's tint with a per-mode pulse (no-op if unchanged).

        - bash: quick, urgent magenta pulse
        - plan: slow, calm blue "breathing"
        - None: stop and clear the tint
        """
        if mode == self._input_pulse_mode:
            return
        self._input_pulse_mode = mode

        if self._input_pulse_timer is not None:
            self._input_pulse_timer.stop()
            self._input_pulse_timer = None

        try:
            prompt = self.query_one("#prompt", PromptInput)
        except Exception:  # noqa: BLE001
            return

        if mode is None:
            # Smoothly fade the tint away.
            prompt.styles.animate("tint", value=Color(0, 0, 0, 0.0), duration=0.3)
            return

        if mode == "bash":
            glow = Color.parse("#bb9af7")
            period = 0.55  # fast, alert
            peak = 0.22
        else:  # plan
            glow = Color.parse("#7aa2f7")
            period = 1.1  # slow, calm
            peak = 0.16

        self._pulse_on = False

        def _tick() -> None:
            self._pulse_on = not self._pulse_on
            alpha = peak if self._pulse_on else 0.02
            try:
                prompt.styles.animate("tint", value=glow.with_alpha(alpha), duration=period * 0.85)
            except Exception:  # noqa: BLE001
                pass

        _tick()  # kick off immediately
        self._input_pulse_timer = self.set_interval(period, _tick)

    # -- autocomplete dropdown ------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        self._feed_palette(event.value, event.input.cursor_position)
        self._update_mode_badge(event.value)

    def on_text_area_changed(self, event: Any) -> None:
        """The prompt is a TextArea, which posts Changed instead of
        Input.Changed. Feed the palette/mode badge the same way."""
        area = getattr(event, "text_area", None)
        if area is None or getattr(area, "id", None) != "prompt":
            return
        text = area.text
        # Palette completion is line-oriented; give it the offset within
        # the cursor's own line so "@" detection behaves as before.
        try:
            row, col = area.cursor_location
            line = text.split("\n")[row]
        except Exception:  # noqa: BLE001
            line, col = text, len(text)
        self._feed_palette(line, col)
        self._update_mode_badge(text)

    def _feed_palette(self, line: str, col: int) -> None:
        """Run completion only when the line can have completions.

        Candidates exist only for a leading ``/`` or an ``@token`` at the
        cursor. Every other keystroke — i.e. ordinary typing — used to start a
        worker, sleep 50 ms, hop to a thread and clear an empty list anyway.
        """
        if not self._autocomplete_enabled:
            self.workers.cancel_group(self, "palette")
            self._hide_palette()
            return
        if line.startswith("/") or self._active_at_fragment(line, col) is not None:
            self._update_palette(line, col)
        else:
            self.workers.cancel_group(self, "palette")  # a stale search must not reopen it
            self._hide_palette()

    def _active_at_fragment(self, value: str, cursor: int) -> tuple[int, str] | None:
        """The ``@token`` ending at the cursor, anywhere in the line.

        Returns ``(start_index_of_@, fragment_after_@)`` or ``None``. The token
        must be at the start of the line or preceded by whitespace, so emails
        (``user@host``) and mid-word ``@`` don't trigger completion.
        """
        m = _AT_FRAGMENT_RE.search(value[: max(0, cursor)])
        if not m:
            return None
        return m.start(), m.group(1)

    def _palette_candidates(self, value: str, cursor: int | None = None) -> list[str]:
        """Completion candidates for the current input, by trigger context.

        ``@`` mentions are matched at the **cursor token anywhere** in the line;
        ``/`` commands remain line-level (they only make sense at the start).
        """
        if cursor is None:
            cursor = len(value)

        # @<agent>/<file> — completes the @token under the cursor, ANYWHERE.
        frag = self._active_at_fragment(value, cursor)
        if frag is not None:
            return self._at_candidates(frag[1])

        # Slash contexts are line-level: a space means the token is complete,
        # unless it is a command like `/ingest` or `/file` that takes arguments.
        if " " in value or not value:
            if value.startswith("/ingest "):
                prefix = value[len("/ingest ") :].strip()
                try:
                    from novacode_cli.wiki.ingest import IngestEngine
                    from pathlib import Path

                    engine = IngestEngine()
                    sources = engine.list_raw_sources()
                    matches = []
                    for s in sources:
                        if (
                            not prefix
                            or prefix.lower() in s.lower()
                            or prefix.lower() in Path(s).name.lower()
                        ):
                            matches.append(s)
                    return [f"/ingest {s}" for s in matches[:50]]
                except Exception:
                    return []
            elif value.startswith("/file "):
                prefix = value[len("/file ") :].strip()
                categories = [
                    "technologies/",
                    "frameworks/",
                    "patterns/",
                    "projects/",
                    "comparisons/",
                ]
                matches = [
                    c for c in categories if not prefix or c.lower().startswith(prefix.lower())
                ]
                return [f"/file {c}" for c in matches]
            return []
        v = value.lower()
        # Legacy /skill:<name> form — still works, kept for back-compat.
        if value.startswith("/skill:"):
            return [
                f"/skill:{n}"
                for n in self._get_skill_names()
                if f"/skill:{n}".lower().startswith(v)
            ]
        # /<command> or bare /<skill-name>. Skills are invocable directly as
        # /<name> (resolved in _run_slash via _run_skill), so surface them here
        # alongside the built-in commands; a skill sharing a command's name
        # shouldn't appear twice.
        if value.startswith("/"):
            cmds = [c for c in _TUI_SLASH_COMMANDS if c.startswith(v)]
            seen = set(cmds)
            skill_cmds = [
                f"/{n}"
                for n in self._get_skill_names()
                if f"/{n}".lower().startswith(v) and f"/{n}" not in seen
            ]
            return cmds + skill_cmds
        return []

    def _at_candidates(self, fragment: str) -> list[str]:  # noqa: PLR0915 (budgeted BFS walk)
        """Build @agent + @file completions for an ``@`` fragment (no leading @)."""
        at_prefix = f"@{fragment}".lower()
        candidates: list[str] = []
        # Agent completions
        for n in self._get_agent_names():
            if f"@{n}".lower().startswith(at_prefix):
                candidates.append(f"@{n}")
        # File completions — recursive match across the whole project tree
        if True:
            prefix = fragment
            max_results = 50
            try:
                from novacode_cli.config.config import settings

                cwd = settings.get_workspace_root()
                # Skip common non-source directories to keep rglob fast
                _SKIP_DIRS = frozenset(
                    {
                        ".git",
                        ".nova",
                        ".venv",
                        ".env",
                        "node_modules",
                        "__pycache__",
                        ".pytest_cache",
                        "build",
                        "dist",
                        ".ruff_cache",
                        ".mypy_cache",
                    }
                )

                # Cap entries scanned so a rare/no-match prefix can't walk the
                # whole repo. _update_palette runs this in a thread its exclusive
                # worker can't actually interrupt, so an unbounded walk keeps
                # churning (and contends the GIL) after every keystroke.
                max_scan = 4000
                scanned = 0

                def _walk(start: Path, prefix: str, cwd: Path, seen: set[str]) -> None:
                    """Match files starting with prefix, breadth-first under start.

                    BFS (not recursion) so shallow files — the ones usually wanted
                    — are matched first and the scan budget caps deep exploration;
                    a DFS budget could be exhausted descending one huge subtree
                    before ever reaching a root-level match.
                    """
                    nonlocal scanned
                    from collections import deque

                    queue = deque([start])
                    while queue and scanned < max_scan and len(seen) < max_results:
                        for child in queue.popleft().iterdir():
                            if scanned >= max_scan or len(seen) >= max_results:
                                break
                            scanned += 1
                            is_dir = child.is_dir()
                            # Skip hidden / noise dirs (don't descend into them)
                            if is_dir and (child.name.startswith(".") or child.name in _SKIP_DIRS):
                                continue
                            if child.name.lower().startswith(prefix.lower()):
                                rel = child.relative_to(cwd).as_posix()
                                tag = f"@{rel}"
                                if is_dir:
                                    tag += "/"
                                if tag not in seen:
                                    seen.add(tag)
                                    candidates.append(tag)
                            if is_dir:
                                queue.append(child)

                seen: set[str] = set()
                if "/" in prefix:
                    dir_part, _, file_part = prefix.rpartition("/")
                    search_dir = (cwd / dir_part).resolve()
                    if search_dir.is_dir():
                        for p in search_dir.iterdir():
                            name = p.name
                            if name.lower().startswith(file_part.lower()):
                                rel = p.relative_to(cwd).as_posix()
                                tag = f"@{rel}"
                                if p.is_dir():
                                    tag += "/"
                                if tag not in seen:
                                    seen.add(tag)
                                    candidates.append(tag)
                            if len(seen) >= max_results:
                                break
                else:
                    _walk(cwd, prefix, cwd, seen)
            except Exception:
                pass
            return candidates

    @work(group="palette", exclusive=True)
    async def _update_palette(self, value: str, cursor: int | None = None) -> None:
        if cursor is None:
            cursor = len(value)

        # Small debounce for fast typing.
        await asyncio.sleep(0.05)

        # Run the potentially heavy candidate search (which walks the filesystem)
        # in a background thread to keep the main TUI loop responsive.
        matches = await asyncio.to_thread(self._palette_candidates, value, cursor)

        # No-op (don't show) when the only match already equals the current
        # token — the @fragment under the cursor, or the whole line for slashes.
        frag = self._active_at_fragment(value, cursor)
        current_token = f"@{frag[1]}" if frag is not None else value
        show = bool(matches) and not (
            len(matches) == 1 and matches[0].lower() == current_token.lower()
        )
        if not show:
            self._hide_palette()
            return
        # No-op when the candidate list is identical to what's already shown —
        # this runs on every keystroke, and rebuilding the OptionList (clear +
        # re-add) every time is the bulk of typing lag in completion contexts.
        if matches == self._last_palette:
            return
        self._last_palette = list(matches)
        try:
            palette = self._w("#cmdpalette", OptionList)
        except NoMatches:
            return
        palette.clear_options()
        for c in matches:
            palette.add_option(Option(c))
        palette.display = True
        try:
            palette.highlighted = 0
        except Exception:  # noqa: BLE001
            pass

    def _hide_palette(self) -> None:
        self._last_palette = None
        try:
            palette = self._w("#cmdpalette", OptionList)
        except NoMatches:
            return
        if not palette.display and not palette.option_count:
            return  # already hidden and empty: nothing to repaint or re-lay-out
        palette.clear_options()
        palette.display = False

    def _accept_palette(self, command: str) -> None:
        inp = self.query_one("#prompt", PromptInput)
        value = inp.value
        cursor = inp.cursor_position
        frag = self._active_at_fragment(value, cursor)
        if frag is not None and command.startswith("@"):
            # Replace only the @token under the cursor, preserving the rest of
            # the line. Directories (trailing "/") get no space so the user can
            # keep typing the path; everything else gets a trailing space.
            start, _ = frag
            trailing = "" if command.endswith("/") else " "
            inp.value = value[:start] + command + trailing + value[cursor:]
            inp.cursor_position = start + len(command) + len(trailing)
        else:
            # Slash command (line-level): replace the whole line.
            inp.value = f"{command} "
            inp.cursor_position = len(inp.value)
        self._hide_palette()
        inp.focus()

    def on_key(self, event) -> None:
        # Runs on EVERY keystroke — use the cached ref and bail immediately when
        # the palette is hidden (the common case) to avoid a DOM walk per key.
        try:
            palette = self._w("#cmdpalette", OptionList)
        except NoMatches:
            return
        if not palette.display:
            return
        if event.key == "down":
            palette.action_cursor_down()
        elif event.key == "up":
            palette.action_cursor_up()
        elif event.key == "escape":
            self._hide_palette()
            event.stop()
            event.prevent_default()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Mouse-click accept from the command palette (other lists handle their own).
        if event.option_list.id == "cmdpalette":
            self._accept_palette(str(event.option.prompt))

    def _on_large_paste(self, placeholder: str, char_count: int) -> None:
        """Notify the transcript that a large paste was collapsed."""
        self._log(Text(f"{placeholder} ({char_count:,} chars)", style="dim"))

    def _on_clipboard_image(self, image: ImageData) -> str:
        """Register a Ctrl+V clipboard image and return its placeholder id.

        Returns ``""`` when images aren't tracked, so the widget falls back to a
        text paste instead of inserting a placeholder that resolves to nothing.

        The image joins the *conversation's* images: ``prepare_input_content``
        re-attaches every tracked image to each turn, so it stays available for
        follow-up questions until ``/clear`` (or ``/images clear``) drops it.
        """
        tracker = self.image_tracker
        if tracker is None:
            return ""
        try:
            image_id = tracker.add_image(image)
        except Exception:  # noqa: BLE001 — pasting must never break input
            logger.warning("clipboard image could not be tracked", exc_info=True)
            return ""
        try:
            size_kb = image.size_kb
        except Exception:  # noqa: BLE001
            size_kb = 0.0
        self._log(
            Text(
                f"🖼️  Image pasted: {image_id} ({size_kb:.1f} KB) — "
                "attached to this conversation (/images to manage)",
                style="dim",
            )
        )
        return image_id

    def on_prompt_input_submitted(self, event: Any) -> None:
        """The prompt (a TextArea) posts its own Submitted on enter."""
        self.on_input_submitted(event)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Only react to the main prompt (modals have their own inputs).
        if event.input.id != "prompt":
            return
        # If the palette is open, Enter accepts the highlighted command.
        palette = self.query_one("#cmdpalette", OptionList)
        if palette.display and palette.option_count and palette.highlighted is not None:
            opt = palette.get_option_at_index(palette.highlighted)
            self._accept_palette(str(opt.prompt))
            return
        # The input box holds compact [paste #N +M lines] placeholders for large
        # pastes (so the box stays readable while composing). On submit we expand
        # them back to the full text, which is what both the agent receives AND
        # what the chat shows — the sent message is displayed in full.
        text = resolve_paste_placeholders(event.value, self.paste_tracker).strip()
        # Strip deceptive/invisible Unicode (BiDi overrides, zero-width chars) a
        # paste may smuggle in — a prompt-injection vector. Warn + sanitize.
        text = self._sanitize_user_text(text)
        if not text:
            return
        event.input.value = ""
        self._hide_palette()
        # Submitting is an explicit act: resume following the tail so the user
        # always sees their own message, even if they had scrolled up to read.
        self._follow_tail = True
        # While the agent is working, a submitted prompt steers the current run
        # (injected for its next step) instead of cancelling it or starting a new
        # turn. Esc still cancels. Empty turns route normally.
        if self._turn_active:
            # Exit is a control command, not a prompt: honour it immediately
            # rather than queueing behind a turn that may run for minutes. This
            # handler is sync, so the async quit runs as a worker. A separate
            # group stops the "turn" group's cancellation (issued inside
            # action_quit) from killing the quit itself mid-flight.
            if text.strip().lower() in _EXIT_COMMANDS:
                self.run_worker(self.action_quit(), group="quit", exclusive=True)
                return
            # A slash command or !bash isn't a message to the agent — queue it to
            # RUN as a command when the turn ends, rather than steering the agent
            # with its literal text.
            stripped = text.lstrip()
            command = stripped.split(maxsplit=1)[0].lower() if stripped else ""
            if command == "/steer":
                # Steering is the exception: it updates the shared instruction
                # list the active agent reads before its next model call. Running
                # it now lets guidance typed during a tool call affect the model
                # immediately after that tool returns.
                self.run_worker(self._run_steer(text), group="live_steer_command", exclusive=False)
                return
            command_name = command[1:] if command.startswith("/") else ""
            command_name = _TUI_COMMAND_ALIASES.get(command_name, command_name)
            if command_name == "context" and len(stripped.split()) > 2:
                # Changing an imported-context mode updates the active graph
                # state, so defer that operation until the current turn settles.
                command_name = ""
            if command_name in _LIVE_UI_COMMANDS:
                # Independent UI and status commands stay usable while the
                # agent works; use a separate non-exclusive worker so modal
                # screens don't block or cancel the active turn.
                if command_name == "remote":
                    coroutine = self._run_remote(text)
                elif command_name == "theme":
                    coroutine = self._run_theme()
                elif command_name == "help":
                    self._run_help()
                    return
                else:
                    coroutine = self._run_slash(text)
                self.run_worker(coroutine, group="live_ui_command", exclusive=False)
                return
            if stripped.startswith("/") or stripped.startswith("!"):
                self._deferred_commands.append(text)
                self._log(Text(f"↳ Queued command (runs after this turn): {text}", style="dim"))
            else:
                self._add_live_steer(text)
            return
        self._dispatch(text)

    def _sanitize_user_text(self, text: str) -> str:
        """Strip deceptive/invisible Unicode from user input (warn + sanitize).

        Hidden BiDi/zero-width characters in a prompt (often pasted) can hide
        instructions from the human while still reaching the model. We remove
        them and surface a TUI notice. Best-effort — never block input on error.
        """
        try:
            from novacode_cli.security.unicode_security import (
                detect_dangerous_unicode,
                strip_dangerous_unicode,
                summarize_issues,
            )

            issues = detect_dangerous_unicode(text)
            if not issues:
                return text
            cleaned = strip_dangerous_unicode(text)
            self._log(
                Text(
                    f"🛡 Removed {len(issues)} hidden Unicode char(s) from input "
                    f"({summarize_issues(issues)})",
                    style="yellow",
                )
            )
            return cleaned
        except Exception:  # noqa: BLE001
            return text

    def _add_live_steer(self, text: str) -> None:
        """Inject a transient steering instruction into the in-flight turn.

        The agent's SteeringMiddleware reads ``session_state.steering_instructions``
        on every model call, so appending here makes the running agent pick this
        up at its next step. The instruction is removed when the turn ends
        (one-turn lifetime) so it doesn't leak into later turns.
        """
        from novacode_cli.bootstrap.steering import SteeringInstruction

        if getattr(self.session_state, "steering_instructions", None) is None:
            self.session_state.steering_instructions = []
        si = SteeringInstruction(label="steer", instruction=text)
        self.session_state.steering_instructions.append(si)
        self._live_steers.append(si)
        self._log(
            Text(
                f"↗ Steering (applies on the next step): {text}",
                style="italic #7aa2f7",
            )
        )

    def action_cancel_turn(self) -> None:
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "child":
            self.run_worker(self._supervisor().cancel(pane.sid))
            self._set_status("cancelling…")
            return
        # Kill any in-flight foreground shell/execute subprocess FIRST. It runs in
        # a detached thread+loop that worker cancellation can't reach, so without
        # this a hung command keeps running (and freezes/crashes the UI) until its
        # internal timeout. The middleware's read loop polls this and terminates.
        try:
            from novacode_cli.shell.jobs import request_kill

            if request_kill():
                self._set_status("killing command…")
        except Exception:  # noqa: BLE001
            pass

        # A spawned session runs in its own process — cancel THAT, not our workers.

        # A turn started from a remote bridge runs inside the "remote" consumer
        # worker, not the "turn" group, so the group cancel below never reached
        # it — escape did nothing for a Telegram/Discord-triggered turn. Cancel
        # the tracked task instead of the group: the group cancel would take the
        # message loop with it and detach the bridge.
        remote_turn = getattr(self, "_remote_turn_task", None)
        if remote_turn is not None and not remote_turn.done():
            remote_turn.cancel()

        # Cancel only the turn (and speech), NOT every worker: cancel_all() would
        # also kill the supervisor's child-output readers and the remote consumer,
        # silently detaching every background session.
        self.workers.cancel_group(self, "turn")
        self.workers.cancel_group(self, "voice_tts")
        self._set_status("cancelling…")

    # ── Voice I/O ─────────────────────────────────────────────────────────

    def _ensure_voice_pipeline(self) -> bool:
        """Build the voice pipeline if needed; return whether voice is usable."""
        from novacode_cli import audio

        if not audio.is_voice_available():
            return False
        if self._voice_pipeline is None:
            from novacode_cli.config.nova_config import NovaConfig

            cfg = NovaConfig().get_voice_config()
            if getattr(self.session_state, "_voice_pipeline", None) is not None:
                self._voice_pipeline = self.session_state._voice_pipeline
            else:
                from novacode_cli.audio.pipeline import VoicePipeline

                self._voice_pipeline = VoicePipeline(
                    stt_provider=cfg.get("stt_provider", "faster-whisper"),
                    tts_provider=cfg.get("tts_provider", "piper"),
                    provider_configs=cfg.get("providers", {}),
                    stt_model=cfg.get("stt_model", "base"),
                    stt_device=cfg.get("stt_device", "auto"),
                    tts_voice=cfg.get("tts_voice", "en_US-lessac-medium"),
                )
            self._voice_speak_responses = bool(cfg["speak_responses"])
            # Voice providers stay lazy: speaking must not preload input models.
        return True

    @work(group="voice_warmup", exclusive=True)
    async def _voice_warmup(self) -> None:
        """Background pre-load of the voice models (best-effort, never crashes).

        If any model still needs downloading, surface a status-line indicator
        while warmup runs so launching Nova doesn't look frozen.
        """
        if self._voice_pipeline is None:
            return

        pending: list[str] = []
        with suppress(Exception):
            pending = self._voice_pipeline.downloads_pending()
        if pending:
            self._set_nova_indicator(
                f"⬇ downloading voice models… ({', '.join(pending)})",
                style="dim cyan",
            )
        # warmup() already swallows per-model errors; this guards the call itself.
        with suppress(Exception):
            await self._voice_pipeline.warmup()
        if pending:
            self._set_nova_indicator("● voice models ready", style="dim green", auto_clear=3.0)

    async def _eager_voice_warmup(self) -> None:
        """Initialize enabled voice without preloading models.

        Default push-to-talk and reply preferences do not mean voice is enabled.
        Reading configuration stays off the UI loop.
        """
        from novacode_cli.config.nova_config import NovaConfig

        cfg = await asyncio.to_thread(lambda: NovaConfig().get_voice_config())
        voice_wanted = bool(cfg.get("enabled"))
        if voice_wanted:
            self._ensure_voice_pipeline()

    def _voice_unavailable_notice(self) -> None:
        self._set_nova_indicator(
            "🎤 voice not installed — see /voice status",
            style="yellow",
            auto_clear=4.0,
        )

    def _submit_voice_text(self, text: str) -> None:
        """Route a transcript like typed input: steer an active turn, else dispatch."""
        text = self._sanitize_user_text(text)
        if not text:
            return
        if self._turn_active:
            self._add_live_steer(text)
        else:
            self._dispatch(text)

    async def action_voice_talk(self) -> None:
        """ctrl+g: capture one spoken utterance (VAD-endpointed) and submit it."""
        if not self._ensure_voice_pipeline():
            self._voice_unavailable_notice()
            return
        if self._voice_capturing:
            return  # debounce double-press
        self.workers.cancel_group(self, "voice_tts")
        self._voice_capturing = True
        self._set_nova_indicator("🎤 listening…", style="bold cyan")
        transcript: str | None = None
        try:
            transcript = await self._voice_pipeline.capture_utterance()
        except Exception as _mic_err:  # noqa: BLE001 — a mic/STT error must never crash the TUI
            msg = str(_mic_err)
            short = msg[:80].replace("\n", " ")
            self._set_nova_indicator(f"🎤 mic error: {short}", style="red", auto_clear=4.0)
            self._log(
                Text(
                    f"[🎤 Mic error] {msg[:200]}",
                    style="red",
                )
            )
            return
        finally:
            self._voice_capturing = False
        if transcript:
            self._set_nova_indicator("")
            self._submit_voice_text(transcript)
        else:
            self._set_nova_indicator("🎤 (nothing heard)", style="dim", auto_clear=2.0)

    async def action_voice_toggle(self) -> None:
        """ctrl+shift+v: toggle hands-free always-listening mode."""
        if not self._ensure_voice_pipeline():
            self._voice_unavailable_notice()
            return
        if self._voice_listening:
            self._voice_listening = False
            self.workers.cancel_group(self, "voice")
            self.workers.cancel_group(self, "voice_tts")
            self._set_nova_indicator("🎤 voice off", style="dim", auto_clear=2.0)
            return
        self._voice_listening = True
        self._set_nova_indicator("🎤 listening (hands-free)…", style="bold cyan")
        self._voice_listen_loop()

    @work(group="voice", exclusive=True)
    async def _voice_listen_loop(self) -> None:
        """Continuously capture utterances and submit each; pauses during TTS."""
        if not self._ensure_voice_pipeline():
            return
        try:
            await self._voice_pipeline.listen_loop(
                self._submit_voice_text,
                should_stop=lambda: not self._voice_listening,
            )
        except Exception as _listen_err:  # noqa: BLE001 — never let the audio loop crash the TUI
            self._voice_listening = False
            msg = str(_listen_err)
            short = msg[:80].replace("\n", " ")
            self._set_nova_indicator(f"🎤 listen error: {short}", style="red", auto_clear=4.0)
            self._log(
                Text(
                    f"[🎤 Listen error] {msg[:200]}",
                    style="red",
                )
            )

    @work(group="voice_tts")
    async def _speak_reply(self, text: str) -> None:
        """Speak a natural 1-2 sentence summary of an assistant reply via TTS.

        No-op unless voice has been activated this session and ``speak_responses``
        is on. The reply is condensed into a short spoken summary (out-of-band
        LLM call, fail-open); short replies are spoken as-is.
        """
        pipeline = self._voice_pipeline
        if pipeline is None or not self._voice_speak_responses:
            return
        from novacode_cli.audio.summarize import summarize_for_speech

        prose = await summarize_for_speech(text)
        if not prose:
            return

        # Re-check self._voice_pipeline in case it was reset during the await
        pipeline = self._voice_pipeline
        if pipeline is None:
            return

        # Check if the voice model needs to be downloaded before speaking
        if getattr(pipeline, "tts_needs_download", False):
            self._set_nova_indicator("🔊 downloading voice…", style="dim cyan")
            # Force the pipeline warmup (downloads the voice model)
            try:
                await pipeline.warmup(input_audio=False)
            except Exception as w_err:  # noqa: BLE001
                self._log(Text(f"[🔊 TTS error] Voice download failed: {w_err}", style="red"))
                self._set_nova_indicator("🔊 tts error", style="red", auto_clear=3.0)
                return

        # Re-check again after warmup just in case
        pipeline = self._voice_pipeline
        if pipeline is None:
            return

        self._set_nova_indicator("🔊 speaking…", style="dim cyan")
        rain = self._matrix_rain()
        if rain is not None:
            rain.pause()
        spoke = True
        try:
            try:
                async with self._speech_lock:
                    await pipeline.speak(prose)
            except Exception as tts_err:  # noqa: BLE001 — TTS failure must never crash the TUI
                spoke = False
                msg = str(tts_err)
                self._log(
                    Text(
                        f"[🔊 TTS error] {msg}",
                        style="red",
                    )
                )
        finally:
            if self._os_focused:
                rain = self._matrix_rain()
                if rain is not None:
                    rain.resume()
        if not spoke:
            self._set_nova_indicator("🔊 tts error", style="red", auto_clear=3.0)
        elif self._voice_listening:
            self._set_nova_indicator("🎤 listening (hands-free)…", style="bold cyan")
        else:
            self._set_nova_indicator("")

    async def action_toggle_terminal(self) -> None:
        """ctrl+t: open a new inline interactive terminal widget in the chat transcript."""
        await self._run_bash("!")

    async def action_run_background(self) -> None:
        """ctrl+b: run the current input in the background without blocking the terminal.

        Mirrors Claude Code's Ctrl+B behaviour:
        - ``!<cmd>`` → background subprocess (output streams into a card).
        - Any other text → background agent turn (full agent, fresh thread_id,
          auto-approved tools so it never blocks waiting for user input).
        """
        if getattr(self, "_tasks_panel_open", False) or any(
            isinstance(screen, BackgroundTasksScreen) for screen in self.screen_stack
        ):
            return
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "child":
            self.run_worker(self._send_job_control(pane, "detach", None))
            self._open_tasks_panel()
            return
        try:
            prompt_widget = self._w("#prompt", PromptInput)
        except NoMatches:
            return
        from novacode_cli.shell.jobs import get_current, request_detach

        # A running command takes precedence over any draft in the editor.
        # Preserve that draft; Ctrl+B is a handoff, not a prompt submission.
        if request_detach():
            if self._turn_active:
                self._detach_cancelling = True
                self._set_status("backgrounding command…")
                self.workers.cancel_group(self, "turn")
            return
        current = get_current()
        if current is not None and current.detach.is_set():
            return  # Repeated keypress during handoff must not submit the draft.
        raw = prompt_widget.value.strip()
        if not raw:
            # Context-sensitive Ctrl+B with an empty prompt:
            #  • a command is running  → detach IT to the background (the
            #    registry "started" event logs the task id + updates the ⚙ bar).
            #  • nothing running       → open the Background Tasks panel.
            self._open_tasks_panel()
            return
        prompt_widget.value = ""
        self._update_mode_badge()
        self._bg_job_count += 1
        job_id = self._bg_job_count
        if raw.startswith("!"):
            cmd = raw[1:].strip()
            if not cmd:
                self._log(Text("ctrl+b: empty command after !", style="dim"))
                return
            self._bg_shell_worker(cmd, job_id)
        else:
            from novacode_cli.tui.background_tasks import AgentTask

            task = AgentTask(f"agent-{job_id}", raw)
            self._bg_agent_tasks[task.task_id] = task
            task.worker = self._bg_agent_worker(raw, job_id)
            self._refresh_tasks_bar()

    def _background_agent_tasks(self, pane=None) -> list[Any]:
        """Local Ctrl+B turns and remote async tasks, kept separate from shell jobs."""
        pane = pane or getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "child":
            return []
        from novacode_cli.tui.background_tasks import AgentTask

        tasks = list(self._bg_agent_tasks.values())
        for task in tasks:
            if task.worker is not None and task.status == "running":
                if task.worker.is_cancelled:
                    task.finish("terminated")
                elif task.worker.is_finished:
                    task.finish("failed" if task.worker.error else "done")
        completed = [task for task in tasks if task.status != "running"]
        for task in completed[:-50]:
            self._bg_agent_tasks.pop(task.task_id, None)
        tasks = list(self._bg_agent_tasks.values())
        watcher = getattr(self, "_async_watcher", None)
        for item in watcher.running_tasks() if watcher is not None else []:
            tasks.append(
                AgentTask(
                    task_id=f"async:{item['task_id']}",
                    command=str(item["agent_name"]),
                    started_at=time.monotonic() - item["runtime"],
                )
            )
        return tasks

    def _clear_background_agents(self, pane=None) -> None:
        for task in self._background_agent_tasks(pane):
            if task.status != "running":
                self._bg_agent_tasks.pop(task.task_id, None)

    # ── Background tasks (persistent indicator + panel) ──────────────────
    async def _send_job_control(self, pane, action, job_id) -> None:
        supervisor = self._supervisor()
        child = supervisor.get(pane.sid)
        if child is not None and child.alive:
            await supervisor._send(child, {"t": "job_control", "action": action, "job_id": job_id})

    def _tasks_registry(self, pane=None):
        pane = pane or getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "child":
            from novacode_cli.sessions.tasks import TabJobRegistry

            if not hasattr(pane, "task_registry"):
                pane.task_registry = TabJobRegistry(
                    lambda action, job_id: self.run_worker(
                        self._send_job_control(pane, action, job_id)
                    )
                )
            return pane.task_registry
        from novacode_cli.shell.jobs import get_registry

        return get_registry()

    async def _child_job_logs(self, pane, task_id):
        import uuid

        if not hasattr(pane, "job_log_waiters"):
            pane.job_log_waiters = {}
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        pane.job_log_waiters[request_id] = future
        try:
            supervisor = self._supervisor()
            child = supervisor.get(pane.sid)
            if child is None or not await supervisor._send(
                child,
                {
                    "t": "job_control",
                    "action": "logs",
                    "job_id": task_id,
                    "request_id": request_id,
                },
            ):
                return None
            return await asyncio.wait_for(future, 5)
        except TimeoutError:
            return None
        finally:
            pane.job_log_waiters.pop(request_id, None)

    def _log_task_output(self, pane, text):
        if pane is not None and pane is not getattr(self, "_active_pane", None):
            self._queue_pane_event(pane, ev.ContextMessage(message=text.plain))
            pane.unread += 1
        else:
            self._log(text)

    def _refresh_tasks_bar(self) -> None:
        """Rebuild the ``●`` indicator: background shell jobs (●) + running async
        subagents (◇), with live runtime + count. Runs on the 1s timer (live
        clock) and on every registry event."""
        active = []
        fmt_runtime = lambda s: f"{int(s)}s"  # noqa: E731 — fallback if jobs import fails
        try:
            from novacode_cli.shell.jobs import fmt_runtime as _fr

            fmt_runtime = _fr
            active = self._tasks_registry().active()
        except Exception:  # noqa: BLE001
            pass
        agents = [
            {"agent_name": task.command, "runtime": task.runtime()}
            for task in self._background_agent_tasks()
            if task.status == "running"
        ]
        try:
            bar = self._w("#tasks-bar", Static)
        except NoMatches:
            return
        if not active and not agents:
            bar.remove_class("active")
            _paint(bar, "")
            # Nothing running → stop the runtime ticker so it never interferes
            # with the rest of the UI when idle.
            if self._tasks_timer is not None:
                try:
                    self._tasks_timer.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._tasks_timer = None
            return
        # Something running → ensure the 1s runtime ticker is running.
        if self._tasks_timer is None:
            try:
                self._tasks_timer = self.set_interval(1.0, self._refresh_tasks_bar)
            except Exception:  # noqa: BLE001
                self._tasks_timer = None
        t = Text()
        pal = self._palette
        total = len(active) + len(agents)
        pane = getattr(self, "_active_pane", None)
        title = getattr(pane, "title", "Main")[:20]
        if total == 1 and active:
            job = active[0]
            t.append(f"● Tasks · {title}  ", style=f"bold {pal.primary}")
            t.append("● ", style=pal.success)
            t.append(f"{job.task_id} {job.command[:40]}", style=pal.text)
            t.append(f"  {fmt_runtime(job.runtime())}", style=pal.dim)
        elif total == 1 and agents:
            a = agents[0]
            t.append(f"◇ Tasks · {title}  ", style=f"bold {pal.accent}")
            t.append("● ", style=pal.accent)
            t.append(str(a["agent_name"])[:36], style=pal.text)
            t.append(f"  {fmt_runtime(a['runtime'])}", style=pal.dim)
        else:
            t.append(f"● Tasks · {title} ({total})  ", style=f"bold {pal.primary}")
            segs = []
            for job in active[:2]:
                segs.append(f"● {job.command.split()[0][:14]} {fmt_runtime(job.runtime())}")
            for a in agents[:2]:
                segs.append(f"◇ {str(a['agent_name'])[:14]} {fmt_runtime(a['runtime'])}")
            t.append("  ·  ".join(segs), style=pal.text)
            shown = min(len(active), 2) + min(len(agents), 2)
            if total > shown:
                t.append(f"  +{total - shown} more", style=pal.dim)
        _paint(bar, t)
        bar.add_class("active")

    def _on_task_event_threadsafe(self, event: str, job: Any) -> None:
        """Registry observer — fires on the background loop thread. Marshal to the
        UI thread (Textual widgets aren't thread-safe)."""
        # "output" fires on every log chunk; the indicator shows only
        # command/runtime/count (runtime advances via the 1s timer), so ignore it
        # here to avoid flooding the UI thread. The panel reads logs directly.
        if event == "output":
            return
        self._post_to_ui(self._on_task_event, event, job)

    def _on_task_event(self, event: str, job: Any) -> None:
        self._refresh_tasks_bar()
        # The process-local registry belongs to the root conversation. Never
        # inject its logs, notes, or completion prompts into a selected child.
        root = getattr(self, "_root_pane", None)
        if root is not None and root is not getattr(self, "_active_pane", None):
            if job is not None and event in ("started", "completed", "failed", "terminated"):
                message = (
                    f"Running in background: {job.command[:70]} · {job.task_id}"
                    if event == "started"
                    else f"Task {event}: {job.command[:60]} · {job.task_id} · exit {job.exit_code}"
                )
                self._queue_pane_event(root, ev.ContextMessage(message=message))
                root.unread += 1
                notes = root.state.setdefault("_pending_job_notes", [])
                notes.append(
                    f"Background {job.task_id} {event}. Command: {job.command}. "
                    f"Use get_task_status('{job.task_id}'), get_task_logs('{job.task_id}') "
                    f"or terminate_task('{job.task_id}') to manage it."
                )
                del notes[:-_PENDING_JOB_NOTE_MAX]
                if event != "started":
                    state = root.state.get("session_state")
                    if state is not None:
                        with contextlib.suppress(Exception):
                            state.add_notification(
                                "info", f"Task {event}: {job.task_id}", message, "shell"
                            )
            return
        if event == "started" and job is not None:
            self._log(
                Text.assemble(
                    ("● Running in background: ", f"bold {self._palette.primary}"),
                    (f"{job.command[:70]}\n", self._palette.text),
                    (f"Task ID: {job.task_id}", self._palette.dim),
                )
            )
            # Clarify for the agent on its next turn (a Ctrl+B detach ends the
            # current turn, so the tool call is patched as "cancelled" — this note
            # corrects that: the command is still running as a background task).
            self._queue_pending_job_note(
                f"You moved {job.task_id} to the background; it is still running "
                f"(command: {job.command}). Check it with get_task_status('{job.task_id}') "
                f"or get_task_logs('{job.task_id}'); stop it with terminate_task('{job.task_id}')."
            )
        elif event in ("completed", "failed", "terminated") and job is not None:
            ok = event == "completed"
            glyph = "●" if ok else "✖"
            verb = {"completed": "completed", "failed": "failed", "terminated": "terminated"}[event]
            self._log(
                Text(
                    f"{glyph} Task {verb}: {job.command[:60]} · {job.task_id}"
                    + (f" · exit {job.exit_code}" if job.exit_code is not None else ""),
                    style="green" if ok else "yellow",
                )
            )
            try:
                self.session_state.add_notification(
                    level="info",
                    title=f"Task {verb}: {job.task_id}",
                    message=f"{job.command[:100]} (exit {job.exit_code})",
                    source="shell",
                )
            except Exception:  # noqa: BLE001
                pass
            # Async monitor: a task the agent is waiting on reports back into the
            # conversation as soon as it finishes, instead of leaving a note that
            # sits until the user happens to type again.
            #
            #   resume_on_done   — Ctrl+B detached: the agent was mid-work.
            #   agent_launched   — the agent ran it with background=True.
            #
            # Both are work the agent started and cares about the result of. A
            # task the USER launched or restarted (a dev server, say) is expected
            # to keep running and only leaves a note. Either way this requires an
            # idle agent: resuming mid-turn would interleave two prompts on the
            # same thread.
            watched = getattr(job, "resume_on_done", False) or getattr(job, "agent_launched", False)
            resume = event in ("completed", "failed") and watched and not self._turn_active
            if resume:
                self._log(Text(f"↻ Resuming — {job.task_id} finished.", style="cyan"))
                self._continue_after_task(job)
            else:
                self._queue_pending_job_note(
                    f"Background {job.task_id} {verb} (exit {job.exit_code}). "
                    f"Command: {job.command}. Use get_task_logs('{job.task_id}') for output."
                )

    @work(exclusive=True, group="turn")
    async def _continue_after_task(self, job: Any) -> None:
        """Auto-resume the agent after a Ctrl+B-detached background task finishes,
        feeding it the result so it continues its work from where it left off."""
        if getattr(getattr(self, "_active_pane", None), "kind", "root") != "root":
            self._queue_pending_job_note(
                f"Background {job.task_id} finished; fetch its result with get_task_logs('{job.task_id}')."
            )
            return
        tail = "\n".join(job.output.splitlines()[-40:]) or "(no output)"
        # The Ctrl+B path patches the original tool call as "cancelled", which is
        # misleading on its own — say so only when that actually happened, so an
        # agent-backgrounded task isn't told about a note it never saw.
        detached_note = (
            "If you saw a 'tool call was cancelled' note for that command "
            "earlier, it referred only to the foreground wait being detached; "
            "the command itself ran to completion.\n\n"
            if getattr(job, "resume_on_done", False)
            else ""
        )
        prompt = (
            f"[Background task finished] The command you launched in the "
            f"background — `{job.command}` ({job.task_id}) — has now finished with "
            f"exit code {job.exit_code}. {detached_note}"
            f"Output (last lines):\n{tail}\n\n"
            f"Continue with what you were doing, taking this result into account."
        )
        await self._stream_prompt(prompt)
        await self._maybe_run_approved_plan()

    @work
    async def _open_tasks_panel(self) -> None:
        """Open the Background Tasks panel; handle copy/logs results."""
        if getattr(self, "_tasks_panel_open", False) or any(
            isinstance(screen, BackgroundTasksScreen) for screen in self.screen_stack
        ):
            return
        self._tasks_panel_open = True
        pane = getattr(self, "_active_pane", None)
        registry = self._tasks_registry(pane)
        if hasattr(registry, "refresh"):
            registry.refresh()
        try:
            result = await self.push_screen_wait(
                BackgroundTasksScreen(
                    extra_tasks=lambda: self._background_agent_tasks(pane),
                    clear_extra=lambda: self._clear_background_agents(pane),
                    registry=registry,
                )
            )
        finally:
            self._tasks_panel_open = False
        if not isinstance(result, dict):
            return
        if result.get("action") == "copy":
            cmd = result.get("command", "")
            try:
                self.copy_to_clipboard(cmd)
            except Exception:  # noqa: BLE001
                pass
            self._log_task_output(pane, Text(f"Copied command: {cmd[:70]}", style="dim"))
        elif result.get("action") == "logs":
            task_id = result.get("task_id", "")
            job = registry.resolve(task_id) or next(
                (task for task in self._background_agent_tasks(pane) if task.task_id == task_id),
                None,
            )
            if job is not None:
                output = (
                    await self._child_job_logs(pane, task_id)
                    if pane is not None and pane.kind == "child"
                    else job.output
                )
                tail = (
                    "(logs unavailable)"
                    if output is None
                    else ("\n".join(output.splitlines()[-40:]) or "(no output yet)")
                )
                self._log_task_output(
                    pane,
                    Text.assemble(
                        (
                            f"● {job.task_id} logs ({job.status}):\n",
                            f"bold {self._palette.primary}",
                        ),
                        (tail, self._palette.text),
                    ),
                )

    # ── Artifacts (persistent component) ─────────────────────────────────
    def _refresh_artifacts_component(self) -> None:
        """Update the fixed ``Artifacts (N)`` footer component."""
        try:
            from novacode_cli.artifacts.registry import get_registry

            n = get_registry().count()
        except Exception:  # noqa: BLE001
            n = 0
        pal = self._palette
        if n:
            # The ARTIFACTS label above already carries the icon; repeating it in
            # the value row reads as a stray glyph before the text.
            t = Text(f"Artifacts ({n})", style=f"bold {pal.accent}")
        else:
            t = Text("Artifacts", style=pal.dim)
        self._set_info("#info-artifacts", t)

    def _on_artifact_event_threadsafe(self, event: str, art: Any) -> None:
        """Registry observer — fires on whichever thread created/updated the
        artifact (tools run in worker threads). Marshal to the UI thread."""
        self._post_to_ui(self._on_artifact_event, event, art)

    def _on_artifact_event(self, event: str, art: Any) -> None:
        if event == "created":
            self._log(
                Text(f"◈ Artifact created: {art.title}", style=f"bold {self._palette.accent}")
            )
        self._refresh_artifacts_component()

    @work
    async def _open_artifacts_list(self) -> None:
        """Show the artifact list; open the chosen one in the browser."""
        from novacode_cli.artifacts.registry import get_registry
        from novacode_cli.artifacts.server import artifact_url

        arts = get_registry().list()
        if not arts:
            self._log(
                Text(
                    "No artifacts yet — ask me to create one, e.g. "
                    '"create an artifact showing the changes you made".',
                    style="dim",
                )
            )
            return
        options = [f"◈ {a.title}  ·  [{a.type}] v{a.version} · {a.status}" for a in arts]
        idx = await self.push_screen_wait(
            PickScreen("Artifacts — open in browser", options, hint="↑/↓ · Enter open · Esc cancel")
        )
        if 0 <= idx < len(arts):
            import webbrowser

            url = artifact_url(arts[idx].id)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
            self._log(Text(f"◈ Opening {arts[idx].title} → {url}", style="#7aa2f7"))

    def on_click(self, event: Any) -> None:
        """Handle clicks on the persistent footer components.

        Artifacts list, background-tasks panel, the todo checklist (click
        anywhere on it to collapse/expand), and jump-to-latest.
        """
        try:
            w = getattr(event, "widget", None)
            while w is not None:
                wid = getattr(w, "id", None)
                if wid == "col-artifacts":
                    self._open_artifacts_list()
                    return
                if wid == "tasks-bar":
                    self._open_tasks_panel()
                    return
                if wid == "todo-dock":
                    self.action_toggle_todos()
                    return
                # A retained reasoning card folds/unfolds on click.
                if isinstance(w, ChatMessage) and w.has_class("reason"):
                    w.toggle_collapsed()
                    return
                w = getattr(w, "parent", None)
        except Exception:  # noqa: BLE001
            pass

    async def action_copy_or_quit(self) -> None:
        """ctrl+c: copy the active text selection to the clipboard, else quit.

        Lets users select text in the transcript (chat messages, tool output)
        and copy it with ctrl+c. With no selection, behaves like quit.
        """
        try:
            selected = self.screen.get_selected_text()
        except Exception:  # noqa: BLE001
            selected = None
        if selected:
            try:
                self.copy_to_clipboard(selected)
                self._log(Text(f"📋 Copied {len(selected)} chars to clipboard", style="dim"))
                # Clear the highlight now that it's copied.
                self.screen.selections = {}
                self.screen.refresh()
            except Exception:  # noqa: BLE001
                pass
            return
        await self.action_quit()

    async def action_quit(self) -> None:
        """Persist the session (so --continue works) then exit.

        Every step is independently guarded and the whole sequence is timed, so
        a slow exit can be diagnosed from the log instead of merely felt.
        """
        started = time.monotonic()
        self._release_output_observers()
        # Stop any turn still running. Without this a mid-turn /exit would leave
        # the agent streaming while we tear the app down underneath it.
        await self._cancel_active_turn()
        try:
            from novacode_cli.events import unregister_tool_output_callback

            unregister_tool_output_callback(self._on_tool_output)
        except Exception:  # noqa: BLE001
            pass
        # Shut spawned sessions down before we go: main.py ends in os._exit(),
        # which runs no finally blocks, so anything still alive here is orphaned.
        sup = getattr(self, "_session_supervisor", None)
        if sup is not None:
            with contextlib.suppress(Exception):
                await sup.close_all(timeout=5.0)
        # Same reason, and the bigger leak: MCP stdio servers are child
        # processes (a node/npx tree for playwright, a python one for serena).
        # os._exit() reaps none of them, so every Nova that exited without this
        # left its servers — and their browsers — running forever. Two Nova
        # instances leak twice as fast, which is how a 16 GB machine ends up
        # swapping.
        with contextlib.suppress(Exception):
            from novacode_cli.mcp import get_shared_mcp_middleware

            middleware = get_shared_mcp_middleware()
            pool = getattr(middleware, "_session_pool", None)
            if pool is not None:
                await asyncio.wait_for(pool.aclose(), timeout=5.0)
        # Guarded separately (defence in depth: each already swallows its own
        # errors) so losing the conversation can never follow a teardown fault.
        with contextlib.suppress(Exception):
            await self._save_session()
        with contextlib.suppress(Exception):
            await self._consolidate_learning()
        logger.debug("exit teardown took %.2fs", time.monotonic() - started)
        self.exit()

    async def _cancel_active_turn(self) -> None:
        """Stop the in-flight turn so exit is immediate.

        Reuses ``action_cancel_turn`` rather than cancelling workers directly: it
        also kills a hung foreground shell subprocess and handles a turn started
        from a remote bridge, neither of which a plain group cancel reaches.
        Cancelling only the ``turn`` group (not ``cancel_all``) keeps the
        supervisor's child readers and the remote consumer alive — they are torn
        down by their own steps above.
        """
        if not self._turn_active and not getattr(self, "_remote_turn_task", None):
            return
        try:
            self.action_cancel_turn()
            # Give the cancellation a moment to unwind so teardown does not race
            # a still-running stream, but never block exit on it.
            await asyncio.sleep(0.1)
        except Exception:  # noqa: BLE001 — exiting must not fail on this
            logger.debug("cancel-on-exit failed", exc_info=True)
        self._turn_active = False

    async def _consolidate_learning(self) -> None:
        """Distil this session's un-reviewed work into memory before it ends.

        Hermes reviews on a tool-call threshold, so a session that does real
        work and stops short of it (a handful of edits, then quit) learned
        nothing durable. This runs the same review once on the way out. Purely
        best-effort — exiting must never hang or fail on it.
        """
        try:
            from novacode_cli.hermes.middleware import get_active_learning_middleware

            # LangGraph compiles middleware into the graph and does not expose
            # the instances, so the module publishes the live one.
            mw = get_active_learning_middleware()
            if mw is None:
                return
            if await mw.consolidate_session():
                self._log(Text("● Session learnings consolidated.", style="dim"))
        except Exception:  # noqa: BLE001 — never block exit on learning
            pass

    def _schedule_session_autosave(self) -> None:
        root = getattr(self, "_root_pane", None)
        active = (
            self._turn_active
            if root is getattr(self, "_active_pane", None)
            else bool(root and root.state.get("_turn_active"))
        )
        if self.session_manager is not None and active and not self._autosave_running:
            self._autosave_running = True
            self.run_worker(
                self._periodic_session_save(), group="session-autosave", exit_on_error=False
            )

    async def _periodic_session_save(self) -> None:
        try:
            await self._save_session(
                task_status="interrupted", pane=getattr(self, "_root_pane", None)
            )
        finally:
            self._autosave_running = False

    async def _save_session(
        self,
        *,
        cleared: bool = False,
        pending_prompt: str | None = None,
        task_status: str = "active",
        pane: SessionPane | None = None,
    ) -> None:
        if self.session_manager is None:
            return
        # Serialize checkpoint reads as well as disk writes. A clear/new-session
        # transition must not race a periodic save for the old thread.
        async with self._session_save_lock:
            await self._save_session_snapshot(
                cleared=cleared,
                pending_prompt=pending_prompt,
                task_status=task_status,
                pane=pane,
            )

    async def _save_session_snapshot(
        self,
        *,
        cleared: bool,
        pending_prompt: str | None,
        task_status: str,
        pane: SessionPane | None,
    ) -> None:
        """Save the conversation to disk via the session manager (best effort).

        Args:
            cleared: Mark the saved session as cleared (used by /clear) so it is
                excluded from --continue auto-resume — a cleared conversation
                won't come back, but stays on disk for the picker.
            pending_prompt: Incoming prompt to persist before the graph starts.
            task_status: Recovery status for the snapshot.
            pane: Owning pane, including a root session hidden behind another tab.
        """
        if self.session_manager is None:
            return
        try:
            bundle = (
                pane.state
                if pane is not None and pane is not getattr(self, "_active_pane", None)
                else vars(self)
            )
            session_state = bundle["session_state"]
            agent = bundle["agent"]
            assistant_id = bundle["assistant_id"]
            model_name = bundle["model_name"]
            model_provider = bundle.get("_model_provider")
            thread_id = session_state.thread_id
            session_id = session_state.session_id
            config = {"configurable": {"thread_id": thread_id}}
            # Bound the checkpointer read so a slow/contended DB can't hang /quit.
            try:
                state = await asyncio.wait_for(agent.aget_state(config), timeout=5.0)
                values = state.values or {}
            except Exception:
                if pending_prompt is None:
                    raise
                previous = await asyncio.to_thread(self.session_manager.load_session, session_id)
                values = {"messages": previous.messages if previous else []}
            if thread_id != session_state.thread_id:
                return
            messages = list(values.get("messages", []))
            if pending_prompt is not None:
                from langchain_core.messages import HumanMessage

                messages.append(HumanMessage(content=pending_prompt))
            if not messages:
                return
            from novacode_cli.config.config import settings

            todos = values.get("todos") or getattr(session_state, "todos", None)
            # save_session does several synchronous file writes — run it off the
            # event loop so /save, /clear, and quit don't freeze the UI.
            await asyncio.to_thread(
                self.session_manager.save_session,
                session_id=session_id,
                thread_id=thread_id,
                messages=messages,
                assistant_id=assistant_id,
                todos=todos,
                model_name=model_name,
                model_provider=model_provider,
                project_root=settings.get_workspace_root(),
                sandbox_id=self._sandbox_id,
                sandbox_type=self._sandbox_type,
                cleared=cleared,
                task_status=task_status,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Session recovery save failed", exc_info=True)

    # -- input routing --------------------------------------------------------
    @work(exclusive=True, group="turn")
    async def _dispatch(self, text: str) -> None:
        """Route input: quit / !bash / slash command / @agent / agent prompt."""
        self.workers.cancel_group(self, "voice_tts")

        # Input typed while a spawned session is on screen goes to THAT process.
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "child":
            await self._dispatch_to_child(pane, text)
            return

        low = text.lower()
        if low in _EXIT_COMMANDS:
            await self.action_quit()
            return

        if text.startswith("!"):
            await self._run_bash(text)
            return

        if text.startswith("/"):
            await self._run_slash(text)
            return

        launch_intent = self._parse_natural_launch_intent(text)
        if launch_intent:
            requested_folder, initial_task = launch_intent
            await self._launch_project_session(folder=requested_folder, task=initial_task)
            return

        # @agent mention(s) -> delegate through the main agent's `task` tool.
        try:
            from novacode_cli.config.config import settings
            from novacode_cli.input_utils import (
                parse_agent_mentions,
                parse_agent_mentions_multi,
            )

            mentioned_agents = parse_agent_mentions_multi(text, settings)
            agent_name, query = parse_agent_mentions(text, settings)
        except Exception:  # noqa: BLE001
            # Never break the turn over a mention parse, but do not hide a real
            # bug: a bad import here silently disabled @agent delegation once.
            logger.warning("agent mention parsing failed", exc_info=True)
            mentioned_agents, agent_name, query = [], None, text

        # Two or more agents (or an agent mentioned mid-message): hand the whole
        # request to the main agent and let it orchestrate the named subagents in
        # order via `task`. @file mentions are expanded by the normal turn path.
        if len(mentioned_agents) >= 2 or (  # noqa: PLR2004
            mentioned_agents and agent_name is None
        ):
            ordered = " → ".join(f"@{a}" for a in mentioned_agents)
            await self._add_message(Text(f"You → {ordered}", style="bold cyan"), "user", Text(text))
            preamble = (
                "This request references specialist subagents by @name. Delegate "
                "each part of the work to the named agent using the `task` tool, "
                "in the order implied by the request, passing results (e.g. edited "
                "files) from one to the next. Referenced agents in order: "
                f"{', '.join(mentioned_agents)}.\n\nRequest:\n{text}"
            )
            await self._stream_prompt(preamble)
            return

        # Single agent at the start: delegate directly to that one subagent.
        if agent_name:
            await self._add_message(self._user_label(agent_name), "user", Text(query))
            await self._stream_prompt(
                f"Call the '{agent_name}' subagent to do the following:\n\n{query}"
            )
            return

        # Plain prompt — send it to the agent as a single turn.
        await self._add_message(self._user_label(), "user", Text(text))
        # Surface any background jobs that finished since the last turn (the user
        # sees the clean prompt; the agent sees the note prepended).
        agent_text = text
        if self._pending_job_notes:
            notes = "\n".join(f"- {n}" for n in self._pending_job_notes)
            self._pending_job_notes = []
            agent_text = f"[Background jobs finished since your last turn]\n{notes}\n\n{text}"
        await self._stream_prompt(agent_text)
        # If a plan was approved during this turn, hand off to the main agent.
        await self._maybe_run_approved_plan()

    @staticmethod
    def _parse_natural_launch_intent(text: str) -> tuple[str, str] | None:
        text = text.strip()
        match = re.search(
            r"\b(?:start|open|launch|create)\b.*\b(?:new\s+)?(?:nova\s+)?session\b.*\b(?:in|at)\b\s+(.+?)[?.!]?$",
            text,
            re.IGNORECASE,
        )
        if match:
            phrase = match.group(1).strip().rstrip("?.!")
            task = ""
            if re.search(r"\s+and\s+", phrase, re.IGNORECASE):
                phrase, task = re.split(r"\s+and\s+", phrase, maxsplit=1, flags=re.IGNORECASE)
        else:
            reverse = re.search(
                r"\b(?:open|start|launch|create)\s+(.+?)\s+(?:in|as)\s+"
                r"(?:a\s+)?(?:new\s+)?(?:nova\s+)?session\b(.*)$",
                text,
                re.IGNORECASE,
            )
            if not reverse:
                return None
            phrase, suffix = reverse.group(1).strip(), reverse.group(2).strip().rstrip("?.!")
            suffix = re.sub(
                r"^\s*,?\s*(?:connect|enable)\s+telegram\s*,?\s*",
                "",
                suffix,
                flags=re.IGNORECASE,
            )
            task = re.sub(r"^\s*(?:,?\s*and\s+)?", "", suffix, flags=re.IGNORECASE)
        phrase = re.sub(r"\s+project$", "", phrase, flags=re.IGNORECASE).strip()
        return (phrase, task.strip()) if phrase else None

    async def _stream_prompt(self, text: str, assistant_id: str | None = None) -> None:
        """Run a single prompt through the agent and render its events.

        Serialized on the shared remote lock so local and remote turns never
        interleave on the same checkpointer thread.
        """
        # Remembered so a ContextOverflow can compact and re-send this exact
        # prompt rather than losing the user's message.
        self._last_user_prompt = text
        turn_pane = getattr(self, "_active_pane", None)
        lock = getattr(self.session_state, "_remote_message_lock", None)
        self._reset_streaming()
        self._current_assistant_id = assistant_id
        self._turn_active = True
        self._turn_start = time.monotonic()
        if turn_pane is not None:
            turn_pane.status = "running"
            self._refresh_tabs()
        # New turn: drop the held phrases so it opens with fresh wording. They
        # stay put for the rest of the turn (see the sticky guard in _render).
        status_phrases.reset()
        self._set_status(status_phrases.status_line("thinking", sticky_phrase=True))
        try:
            if self._remote_msg is None:
                await self._start_local_remote_stream(text)
            if lock is not None:
                async with lock:
                    await self._save_session(pending_prompt=text, task_status="interrupted")
                    await self._do_stream(text, assistant_id)
            else:
                await self._save_session(pending_prompt=text, task_status="interrupted")
                await self._do_stream(text, assistant_id)
        except asyncio.CancelledError:
            self._reset_streaming()
            for _target, _status, answer in self._local_remote_streams:
                answer.cancelled = True
            # A Ctrl+B detach surfaces as an ev.Cancelled event (handled with its
            # own "moved to background" note); only a real cancel reaches here.
            if not getattr(self, "_detach_cancelling", False):
                self._log(Text("Cancelled.", style="yellow"))
        except Exception as ex:  # noqa: BLE001
            msg = str(ex).lower()
            for _target, _status, answer in self._local_remote_streams:
                answer.error = str(ex)
            if any(
                kw in msg
                for kw in ("429", "rate limit", "usage limit", "quota", "too many requests")
            ):
                self._log(Text("⚠️ Warning: Rate Limit / Quota Reached", style="bold yellow"))
                self._log(
                    Text(
                        "The model provider is rate-limiting requests or your usage limit is exhausted.",
                        style="yellow",
                    )
                )
                self._log(Text(f"Detail: {ex}", style="dim yellow"))
            elif any(
                kw in msg for kw in ("401", "unauthorized", "api key", "auth", "forbidden", "403")
            ):
                self._log(Text("⚠️ Warning: Authentication / API Key Error", style="bold yellow"))
                self._log(
                    Text("Please verify your API keys or subscription status.", style="yellow")
                )
                self._log(Text(f"Detail: {ex}", style="dim yellow"))
            else:
                self._log(Text(f"Error: {ex}", style="red"))
        finally:
            await self._finish_local_remote_stream()
            self._turn_active = False
            if turn_pane is not None:
                turn_pane.status = "idle"
                self._refresh_tabs()
            if turn_pane is not None and turn_pane is not getattr(self, "_active_pane", None):
                turn_pane.state["_turn_active"] = False
            await self._save_session(pane=turn_pane)
            self._stop_foreground_subagents()
            self._detach_cancelling = False
            self._set_status("ready")
            self._clear_live_steers()
            # Safety net: clear the Nova review indicator if it's still showing
            # (e.g. a review triggered on the final turn never drained its
            # completion event).
            self._set_nova_indicator("")
        # Refresh the per-category context breakdown from agent state, then
        # proactively manage the context window once the turn has settled.
        await self._update_context_breakdown()
        await self._check_context()
        # Run commands queued during the turn (as commands), then any deferred
        # prompts that weren't consumed as steers.
        await self._drain_deferred_commands()
        await self._drain_deferred_prompts()

    async def _update_context_breakdown(self) -> None:
        """Recompute the context breakdown from agent state after a turn.

        The console renderer does this in its finalization step; without it the
        TUI's /context view and context warnings had no per-category detail.
        Best-effort — never blocks the turn on a state read.
        """
        tracker = self.token_tracker
        if tracker is None or not getattr(tracker, "model_name", None):
            return
        try:
            from novacode_cli.context import ContextManager
            from novacode_cli.context.history import effective_messages

            ag, _ = self._active_agent()
            config = {"configurable": {"thread_id": self.session_state.thread_id}}
            state = await ag.aget_state(config)
            self._refresh_router_model_from_state(state.values if state else {})
            msgs = effective_messages(state.values) if state else []
            # Off the loop: it can shell out to `ollama show`, which hangs
            # while the local daemon is busy (a 42s UI freeze was measured).
            model = self._routed_model_name or self.model_name or tracker.model_name
            # Tool schemas are part of every request, even before the first
            # user message and after compaction. Prefer what the active graph
            # binds (plan/init agents can differ), then the session's tools.
            from novacode_cli.ui.execution import _bound_tools

            tools = _bound_tools(ag) or getattr(self.session_state, "_tools", None)
            breakdown = await asyncio.to_thread(
                lambda: ContextManager(model).breakdown(msgs, tools=tools)
            )
            # The injected system prompt is not stored in graph messages.
            # Startup's measured baseline covers that prompt and memory.
            baseline = getattr(tracker, "baseline_context", 0)
            breakdown.system_prompt_tokens += baseline
            breakdown.total_tokens += baseline
            tracker.model_name = model
            tracker.context_window_size = breakdown.context_window_size
            tracker.set_breakdown(breakdown)
        except Exception:  # noqa: BLE001
            logger.debug("Could not rebuild /context metrics from session state", exc_info=True)

        await self._maybe_warn_ollama_offload(tracker.model_name)

    def _refresh_router_model_from_state(self, state_values: dict[str, Any]) -> None:
        """Show the destination model selected by the latest router decision."""
        try:
            from novacode_cli.config.nova_config import NovaConfig

            config = NovaConfig()
            self._router_mode_enabled = config.get_router_enabled()
            if not self._router_mode_enabled:
                self._routed_model_name = None
            else:
                route_id = state_values.get("model_route")
                route = next(
                    (item for item in config.get_router_routes() if item.get("id") == route_id),
                    None,
                )
                self._routed_model_name = str(route.get("model")) if route else None
        except Exception:  # noqa: BLE001 — display refresh is best-effort
            pass
        if self.is_running:
            self._refresh_info_bar()

    async def _maybe_warn_ollama_offload(self, model_name: str | None) -> None:
        """Warn once if the loaded Ollama model is offloaded to CPU (slow).

        Skips cloud API models entirely. Probes `ollama ps` off the event loop;
        stays "unchecked" until the model is actually loaded so the advisory
        still fires on a later turn, then latches off.
        """
        if self._ollama_offload_checked or not model_name:
            return
        from novacode_cli.context._dynamic import is_ollama_cloud_model

        if model_name.lower().startswith(
            ("claude-", "gpt-", "gemini-", "o1", "o3", "o4")
        ) or is_ollama_cloud_model(model_name):
            # Cloud (API or Ollama-cloud) runs remotely — never offloads locally.
            self._ollama_offload_checked = True
            return
        try:
            from novacode_cli.context._dynamic import (
                check_ollama_offloading,
                get_ollama_runtime_info,
            )

            info = await asyncio.to_thread(get_ollama_runtime_info, model_name)
            if info is None:
                return  # not loaded yet — retry on a later turn
            self._ollama_offload_checked = True
            warning = await asyncio.to_thread(check_ollama_offloading, model_name)
            if warning:
                self._log(Text(f"⚠ {warning}", style="bold #e0af68"))
        except Exception:  # noqa: BLE001
            self._ollama_offload_checked = True

    def _clear_live_steers(self) -> None:
        """Drop transient live-steer instructions added during the turn.

        Unconsumed steers (the agent finished before the middleware could
        inject them) are saved to ``_deferred_prompts`` so they can be
        dispatched as a fresh turn rather than silently vanishing.
        """
        if not self._live_steers:
            return
        instrs = getattr(self.session_state, "steering_instructions", None) or []
        for si in self._live_steers:
            # If the middleware never delivered this steer, requeue it.
            if not si.consumed:
                self._deferred_prompts.append(si.instruction)
            try:
                instrs.remove(si)
            except ValueError:
                pass
        self._live_steers.clear()

    async def _drain_deferred_commands(self) -> None:
        """Run slash/bash commands that were queued during the turn — as actual
        commands (routing like _dispatch), not as agent messages."""
        while self._deferred_commands:
            cmd = self._deferred_commands.pop(0)
            self._log(Text(f"↳ Running queued command: {cmd}", style="italic #9ece6a"))
            low = cmd.strip().lower()
            if low in _EXIT_COMMANDS:
                await self.action_quit()
                return
            if cmd.startswith("!"):
                await self._run_bash(cmd)
            elif cmd.startswith("/"):
                await self._run_slash(cmd)

    async def _drain_deferred_prompts(self) -> None:
        """Dispatch prompts that were queued during the previous turn.

        Called after ``_stream_prompt`` finishes. Each deferred prompt is
        shown as a user message and run through the agent as a new turn,
        giving the user seamless "send while busy" behaviour.
        """
        while self._deferred_prompts:
            prompt = self._deferred_prompts.pop(0)
            self._log(
                Text(
                    f"↗ Processing queued message: {prompt}",
                    style="italic #9ece6a",
                )
            )
            await self._add_message(self._user_label(), "user", Text(prompt))
            await self._stream_prompt(prompt)

    async def _check_context(self) -> None:
        """Warn (and optionally auto-compact) as the context window fills up.

        The TUI previously showed ctx% passively but never nudged — so a long
        session could silently approach the model's limit and then error. Here we
        warn once at the warning threshold and, at critical, auto-compact to
        avoid a hard overflow on the next turn.
        """
        if self.token_tracker is None:
            return
        try:
            bd = self.token_tracker.get_breakdown()
        except Exception:  # noqa: BLE001
            return
        if not bd:
            return
        pct = getattr(bd, "usage_percentage", 0.0)
        # Policy lives in novacode_cli/context/pressure.py so the Rich REPL and
        # this TUI cannot drift. Auto-compact fires at AUTO_COMPACT_THRESHOLD
        # (0.82), deliberately below deepagents' 0.85 summarization backstop, so
        # Nova's own compaction wins the race and the library only catches
        # mid-turn overflow.
        from novacode_cli.context import PressureAction, assess_pressure

        decision = assess_pressure(
            pct,
            compacted_last_turn=getattr(self, "_compacted_last_turn", False),
            auto_compact_enabled=self._auto_compact,
            context_window=getattr(bd, "context_window_size", 0) or 0,
        )
        self._compacted_last_turn = decision.compacted_last_turn

        if decision.disable_auto_compact:
            self._auto_compact = False

        if decision.action is PressureAction.COMPACT:
            self._log(Text(f"⚠ {decision.reason}", style="bold #f7768e"))
            await self._run_compact("")
            self._compacted_last_turn = True
            # Floor: if compaction couldn't get us back under the critical line,
            # further auto-compaction is futile (the summary itself is near the
            # window — usually a too-small model). Stop the per-turn loop and
            # tell the user how to recover.
            try:
                bd2 = self.token_tracker.get_breakdown()
            except Exception:  # noqa: BLE001
                bd2 = None
            if bd2 is not None:
                from novacode_cli.context import post_compaction_still_critical

                after = post_compaction_still_critical(
                    getattr(bd2, "usage_percentage", 0.0),
                    auto_compact_enabled=self._auto_compact,
                    context_window=getattr(bd2, "context_window_size", 0) or 0,
                )
                if after.disable_auto_compact:
                    self._auto_compact = False
                    self._log(Text(f"⚠ {after.reason}", style="bold #f7768e"))
            self._ctx_warned = True
        elif decision.action is PressureAction.WARN:
            if decision.disable_auto_compact or not self._auto_compact:
                # Critical-but-disabled: the user must act. Always shown.
                self._log(Text(f"⚠ {decision.reason}", style="bold #f7768e"))
            elif not self._ctx_warned:
                self._log(Text(f"⚠ {decision.reason}", style="#e0af68"))
            self._ctx_warned = True
        else:
            # Dropped back below the warning line (e.g. after /compact) — re-arm.
            self._ctx_warned = False
            self._compacted_last_turn = False

    async def _do_stream(self, text: str, assistant_id: str | None = None) -> None:
        from novacode_cli.input_utils import parse_file_mentions

        if "@" in text:
            _, files = await asyncio.to_thread(parse_file_mentions, text)
            if files:
                self._log(
                    Text("Referenced files: " + ", ".join(path.name for path in files), style="dim")
                )
        ag, backend = self._active_agent()
        aid = assistant_id or self.assistant_id
        async for e in run_agent_stream(
            text,
            ag,
            aid,
            self.session_state,
            backend=backend,
            image_tracker=self.image_tracker,
            seen_message_ids=self._seen,
        ):
            # Through _deliver, not _render directly: if the user switches tabs
            # mid-turn, this turn's events must keep going to the ROOT pane
            # rather than into whichever pane is now on screen.
            await self._deliver(getattr(self, "_root_pane", None), e)

    @work(group="remote")
    async def _remote_consumer(self) -> None:
        """Render remote (Discord/Telegram) prompts in the TUI and reply back.

        Mirrors the legacy remote processor but streams through the TUI instead
        of the console. Turn serialization is handled by ``_stream_prompt``'s
        lock (shared with local input)."""
        import asyncio

        from novacode_cli.remote.processor import _extract_response

        queue = self._remote_owner_state()._remote_message_queue
        while True:
            try:
                msg = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                if await self._remote_route(msg):
                    queue.task_done()
                    continue
                # The main session runs in this process and streams into the
                # visible tab: bring it on screen before its turn starts.
                root = getattr(self, "_root_pane", None)
                if root is not None and getattr(self, "_active_pane", root) is not root:
                    await self._switch_to(root)
                lock = getattr(self.session_state, "_remote_message_lock", None)
                if self._turn_active or (lock is not None and lock.locked()):
                    if getattr(msg, "images", None):
                        await self._defer_remote_image_turn(msg, queue)
                        continue
                    try:
                        # Log it in the TUI transcript so the local user sees it
                        await self._add_message(
                            Text(
                                f"📡 {msg.user_name} ({msg.platform.value})",
                                style="bold cyan",
                            ),
                            "user",
                            Text(msg.text),
                        )
                        # Treat as steer / question response
                        if (
                            self._remote_question_future is not None
                            and not self._remote_question_future.done()
                            and not (getattr(msg, "text", "") or "").lstrip().startswith("/")
                        ):
                            react_fn = getattr(msg, "react_fn", None)
                            if react_fn is not None:
                                try:
                                    await react_fn("📥")
                                except Exception:
                                    pass
                            self._remote_question_future.set_result(msg)
                            continue

                        text = (getattr(msg, "text", "") or "").strip()
                        low = text.lower()
                        if low.startswith("/steer"):
                            text = text[len("/steer") :].strip()
                        elif text.startswith("/"):
                            reply_fn = getattr(msg, "reply_fn", None)
                            if reply_fn is not None:
                                try:
                                    await reply_fn(
                                        "◐ Busy with the current task — send "
                                        "`/steer <text>` (or just text) to add to it."
                                    )
                                except Exception:
                                    pass
                            continue
                        if not text:
                            continue

                        self._add_live_steer(text)
                        react_fn = getattr(msg, "react_fn", None)
                        reply_fn = getattr(msg, "reply_fn", None)
                        if react_fn is not None:
                            try:
                                await react_fn("↗")
                            except Exception:
                                pass
                        elif reply_fn is not None:
                            try:
                                await reply_fn(f"↗ Added to the running task: {text}")
                            except Exception:
                                pass
                    except Exception as ex:
                        self._log(Text(f"Steer error: {ex}", style="red"))
                    finally:
                        queue.task_done()
                    continue
                await self._add_message(
                    Text(
                        f"📡 {msg.user_name} ({msg.platform.value})",
                        style="bold cyan",
                    ),
                    "user",
                    Text(msg.text),
                )
                # A remote sender inherits the user's approval preference.
                config = {"configurable": {"thread_id": self.session_state.thread_id}}
                typing_task: "asyncio.Task | None" = None
                try:
                    self._remote_msg = msg
                    self._remote_activity = []  # tool/subagent names for the status
                    self._remote_status = None
                    self._remote_answer = None
                    self._remote_react("🤔")  # acknowledge: thinking
                    # Keep the "typing…" indicator alive for the whole turn so it
                    # reads like a person typing, then sends a message (the platform
                    # indicator only lasts ~10s, so it must be re-triggered).
                    if msg.typing_fn is not None:

                        async def _typing_loop(typing_fn=msg.typing_fn) -> None:
                            try:
                                while True:
                                    await typing_fn()
                                    await asyncio.sleep(8)
                            except asyncio.CancelledError:
                                return

                        typing_task = asyncio.create_task(_typing_loop())

                    # Slash commands from chat: handle the remote-safe subset
                    # directly (info/toggles/conversation), stream skills as a
                    # turn, and decline interactive/local-only ones.
                    prompt_text = msg.text.strip()
                    slash_reply: str | None = None
                    if prompt_text.startswith("/") and not getattr(msg, "images", None):
                        slash_reply, resolved = await self._remote_slash(prompt_text)
                        if resolved is not None:
                            prompt_text = resolved  # e.g. a resolved skill prompt

                    if slash_reply is not None:
                        # Command fully handled — reply directly, no agent turn.
                        try:
                            await msg.reply_fn(slash_reply)
                        except Exception:  # noqa: BLE001
                            pass
                        self._remote_react("✅")
                    else:
                        from novacode_cli.remote.images import attach_images

                        self.image_tracker = attach_images(
                            self.image_tracker, getattr(msg, "images", [])
                        )
                        pre = await self.agent.aget_state(config)
                        pre_count = len(pre.values.get("messages", [])) if pre else 0
                        # A compact status line edits in place to show live tool/
                        # subagent activity (condensed counts) — SEPARATE from the
                        # answer, which is sent as a fresh chat message below.
                        if getattr(msg, "edit_fn", None) is not None:
                            from novacode_cli.remote.status import RemoteStatusLine

                            self._remote_status = RemoteStatusLine(
                                msg.edit_fn, label=self._remote_label(None)
                            )
                            self._remote_status.start()
                        if getattr(msg, "answer_edit_fn", None) is not None:
                            from novacode_cli.remote.streaming import RemoteAnswerStream

                            self._remote_answer = RemoteAnswerStream(msg.answer_edit_fn)
                            self._remote_answer.start()
                        # While the turn runs, drain further remote messages as
                        # live steers so the user can "add to the previous prompt".
                        steer_drain = asyncio.create_task(self._remote_steer_drain(queue))

                        async def _run_remote_turn() -> None:
                            if isinstance(prompt_text, str):
                                await self._stream_prompt(prompt_text)
                            elif callable(prompt_text):
                                import inspect

                                if inspect.iscoroutinefunction(prompt_text):
                                    await prompt_text()
                                else:
                                    res = prompt_text()
                                    if inspect.iscoroutine(res):
                                        await res

                        # Run the turn as its OWN task so escape can cancel it.
                        # This consumer lives in the "remote" worker group, which
                        # action_cancel_turn deliberately does not touch —
                        # cancelling the group would tear down the message loop
                        # and silently detach the bridge. Without a separate
                        # handle there was nothing escape could cancel, so a
                        # turn started from Telegram/Discord ignored the key.
                        turn_task = asyncio.create_task(_run_remote_turn())
                        self._remote_turn_task = turn_task
                        try:
                            await turn_task
                        finally:
                            self._remote_turn_task = None
                            steer_drain.cancel()
                            try:
                                await steer_drain
                            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                                pass
                        # Settle the status line to a done-summary, then send the
                        # answer as its own message (no footer — the status carries
                        # the tool/subagent summary).
                        if self._remote_status is not None:
                            answer = self._remote_answer
                            await self._remote_status.finalize(
                                outcome="failed"
                                if answer and answer.error
                                else "stopped"
                                if answer and answer.cancelled
                                else "done"
                            )
                        post = await self.agent.aget_state(config)
                        reply = _extract_response(post, pre_count) or "✅ Task completed."
                        if self._remote_label(None):
                            reply = f"**[main]** {reply}"
                        try:
                            streamed = (
                                await self._remote_answer.finalize(reply)
                                if self._remote_answer is not None
                                else False
                            )
                            if not streamed:
                                await msg.reply_fn(reply)
                        except Exception:  # noqa: BLE001
                            pass
                        self._remote_react("✅")
                finally:
                    if self._remote_status is not None:
                        await self._remote_status.finalize(outcome="stopped")
                    if self._remote_answer is not None and not self._remote_answer.closed:
                        self._remote_answer.cancelled = True
                        await self._remote_answer.finalize()
                    self._remote_msg = None
                    self._remote_status = None
                    self._remote_answer = None
                    if typing_task is not None:
                        typing_task.cancel()
                        try:
                            await typing_task
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
            except asyncio.CancelledError:
                self._log(Text("Remote turn cancelled.", style="yellow"))
            except Exception as ex:  # noqa: BLE001
                self._log(Text(f"Remote error: {ex}", style="red"))
                self._remote_react("❌", msg)
            finally:
                with suppress(ValueError):
                    queue.task_done()

    # Slash commands that require the interactive local TUI (modals, pickers,
    # launchers) and can't be driven over a chat bridge.
    _REMOTE_LOCAL_ONLY = frozenset(
        {
            "sessions",
            "mcp",
            "theme",
            "remote",
            "router",
            "agents",
            "skills",
            "init",
            "trello",
            "browser-use",
            "hooks",
            "servers",
            "files",
            "images",
            "vision",
            "kill",
            "restore",
            "reindex",
            "plan",
            "steer",
            "notifications",
            "trace",
            "log",
            "tests",
            "fast",
        }
    )

    def _remote_help_text(self) -> str:
        """Plain-text help listing the commands that work over a remote chat."""
        return (
            "Remote commands:\n"
            "• /help — this list\n"
            "• /context (/tokens, /cost) — context window usage\n"
            "• /model — show the current model\n"
            "• /clear — reset the conversation\n"
            "• /compact — summarize & free up context\n"
            "• /save — save the session\n"
            "• /verbose — toggle settings\n"
            "• /ingest <path> — ingest a raw source into the wiki\n"
            "• /ask <question> — ask with wiki context\n"
            "• /wiki — show Obsidian LLM Wiki browser\n"
            "• /research <query> — launch multi-agent research swarm\n"
            "• /ralph <task> — run autonomously (looping mode)\n"
            "• /evolution — view self-evolution logs\n"
            "• /dream — consolidate memory from previous sessions\n"
            "• /<skill> (e.g. /graphify) — run a skill\n"
            "Sessions (several at once):\n"
            "• /tabs — list sessions · /tab — choose a project and start one\n"
            "• /tab close — choose a running session tab to close\n"
            "• /use <name> — where plain messages go (main = the first one)\n"
            "• @name <text> — send to one session, or reply to its message\n"
            "• In a group with Topics on, each session gets its own topic\n"
            "Anything without a leading / is sent to the agent. Interactive "
            "panels (/model picker, /mcp, /theme…) are local-only."
        )

    async def _remote_slash(self, text: str) -> "tuple[str | None, Any]":
        """Route a slash command arriving from Discord/Telegram.

        Returns ``(reply_text, stream_prompt_or_callable)``:
          * ``(str, None)`` — send this text back; no agent turn.
          * ``(None, str)`` — stream this prompt as an agent turn (skills).
          * ``(None, callable)`` — execute this coroutine/callable in the turn context.
        Interactive / local-only commands return an explanatory reply.
        """
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("help", "?", "commands"):
            return self._remote_help_text(), None
        if cmd in ("tokens", "context", "cost"):
            try:
                return self._token_text().plain, None
            except Exception:  # noqa: BLE001
                return "(token usage unavailable)", None
        if cmd == "model":
            return (
                f"Current model: {self.model_name}\n"
                "Switching models is only available in the local TUI (/model).",
                None,
            )
        if cmd == "verbose":
            new = self.session_state.toggle_verbose()
            return f"Verbose mode {'on' if new else 'off'}.", None
        if cmd == "clear":
            await self._run_clear()
            return "✅ Conversation cleared.", None
        if cmd == "compact":
            await self._run_compact("")
            return "✅ Context compacted.", None
        if cmd == "save":
            await self._run_save()
            return "✅ Session saved.", None
        if cmd == "steer":
            text = arg.strip()
            if text.lower() in ("clear", "reset", "off"):
                self._clear_live_steers()
                return "✅ Steering cleared.", None
            if not text:
                return (
                    "Usage: /steer <instruction> — extra guidance the agent "
                    "follows on its next step (and a running turn picks up now).",
                    None,
                )
            self._add_live_steer(text)
            return f"↗ Steering added: {text}", None

        if cmd == "ingest":
            from novacode_cli.wiki.ingest import IngestEngine

            try:
                engine = IngestEngine()
                if not arg:
                    # Auto-discover the local wiki's Clipping/ + raw/ contents.
                    sources = engine.list_raw_sources()
                    if sources:
                        listing = "\n".join(f"  • {s}" for s in sources)
                        return (
                            "Usage: /ingest <path> (filename found anywhere in "
                            f"Clipping/ or raw/)\nAvailable sources:\n{listing}",
                            None,
                        )
                    return (
                        "No sources yet — save web clips into "
                        f"{engine._mgr.root / 'Clippings'} first.",
                        None,
                    )
                # Resolve the source (Clipping/ or raw/), then stream as a turn.
                source_full = engine.resolve_source(arg)
                rel = source_full.relative_to(engine._mgr.root).as_posix()
                # Off the loop: resolving and reading a source can touch a large
                # file, and this runs while the transcript is live.
                source_content = await asyncio.to_thread(
                    lambda: source_full.read_text(encoding="utf-8")
                )
                prompt = (
                    "Please analyze this source and create a wiki page at "
                    "/.nova/wiki/ for it.\n\n"
                    f"Source ({rel}):\n```\n{source_content[:8000]}\n```"
                )
                return None, prompt
            except (FileNotFoundError, ValueError) as ex:
                return f"Error: {ex}", None
            except Exception as ex:  # noqa: BLE001
                return f"/ingest error: {ex}", None

        if cmd == "ask":
            if not arg:
                return "Usage: /ask <question>", None
            # Search wiki and prepend context
            from novacode_cli.wiki.ask import WikiAskEngine

            try:
                engine = WikiAskEngine()
                prompt = await engine.build_prompt(arg)
                return None, prompt
            except Exception as ex:  # noqa: BLE001
                return f"/ask error: {ex}", None

        if cmd == "research":
            from novacode_cli.commands.research_handler import handle_research_command

            if not arg:
                with _rich_console.capture() as cap:
                    await handle_research_command(
                        self.agent, self.session_state, self.token_tracker, cmd_args=None
                    )
                out = Text.from_ansi(cap.get()).plain.strip()
                return out, None

            from novacode_cli.commands.research_handler import (
                _parse_args,
                _MODE_AGENTS,
                _MODE_DESCRIPTIONS,
            )

            mode, query, agent_count, fast_mode = _parse_args(arg)
            if not query:
                return (
                    f"Error: no research query provided.\nUsage: /research {mode} <your question>",
                    None,
                )

            async def run_res(msg_obj=self._remote_msg):
                self._log(
                    Text(
                        f"📡 Remote ({msg_obj.platform.value if msg_obj else 'Remote'}) triggered research swarm: {query}",
                        style="bold cyan",
                    )
                )
                base_agents = _MODE_AGENTS[mode]
                agents = (base_agents * ((agent_count // len(base_agents)) + 1))[:agent_count]
                base_dir = Path(".nova") / "research"
                try:
                    base_dir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass

                conversation_context = ""
                try:
                    from novacode_cli.context import ContextManager

                    conversation_context = await ContextManager().digest(
                        self.agent, self.session_state.thread_id
                    )
                except Exception:
                    pass

                prompt = render_template(
                    "research_swarm.jinja",
                    research_query=query,
                    mode=mode,
                    mode_description=_MODE_DESCRIPTIONS[mode],
                    agent_count=agent_count,
                    agents=agents,
                    base_dir=base_dir.as_posix(),
                    fast_mode=fast_mode,
                    conversation_context=conversation_context,
                )

                reply_msg = f"🔬 Starting research swarm (mode: {mode}, agents: {agent_count})..."
                if fast_mode:
                    reply_msg += " (fast mode)"
                if msg_obj is not None:
                    try:
                        await msg_obj.reply_fn(reply_msg)
                    except Exception:
                        pass
                await self._tui_execute_fn(
                    prompt,
                    self.agent,
                    "dora",
                    self.session_state,
                    self.token_tracker,
                    self.backend,
                )

            return None, run_res

        if cmd == "evolution":

            async def run_evo(msg_obj=self._remote_msg):
                lines = []

                def _emit(m=""):
                    if m:
                        lines.append(m)

                from novacode_cli.commands.evolution_command import handle_evolution_command

                try:
                    await handle_evolution_command(emit=_emit)
                except ImportError:
                    from novacode_cli.commands.evolution_handler import handle_evolution_command

                    await handle_evolution_command(emit=_emit)
                text_out = "\n".join(lines)
                plain = Text.from_markup(text_out).plain.strip()
                if msg_obj is not None:
                    try:
                        await msg_obj.reply_fn(plain or "No evolution logs yet.")
                    except Exception:
                        pass

            return None, run_evo

        if cmd == "dream":

            async def run_dream(msg_obj=self._remote_msg):
                status_lines = []

                def _emit(m=""):
                    if m:
                        status_lines.append(m)

                from novacode_cli.commands.dream_handler import handle_dream_command

                result = await handle_dream_command(
                    self.session_state, self.assistant_id, emit=_emit
                )
                if status_lines and msg_obj is not None:
                    plain = Text.from_markup("\n".join(status_lines)).plain.strip()
                    try:
                        await msg_obj.reply_fn(plain)
                    except Exception:
                        pass
                if isinstance(result, str) and result.strip():
                    await self._stream_prompt(result)

            return None, run_dream

        if cmd == "ralph":
            # For --status, it's fast, so return directly
            if arg.strip() == "--status":
                lines = []

                def _emit(m=""):
                    if m:
                        lines.append(m)

                from novacode_cli.commands.ralph_handler import handle_ralph_status

                await handle_ralph_status(self.session_state, emit=_emit)
                text_out = "\n".join(lines)
                plain = Text.from_markup(text_out).plain
                return plain, None

            # For running ralph task, run it in the turn context
            async def run_ralph(msg_obj=self._remote_msg):
                self._log(
                    Text(
                        f"📡 Remote ({msg_obj.platform.value if msg_obj else 'Remote'}) triggered autonomous Ralph run: {arg or '(resume)'}",
                        style="bold cyan",
                    )
                )

                # We want to forward ralph's emit events to both the local TUI and the remote user
                async def _emit_remote(message: str = "") -> None:
                    if not message:
                        return
                    try:
                        renderable = Text.from_markup(message)
                    except Exception:
                        renderable = Text(message)

                    # Log to TUI locally
                    self._log(renderable)

                    # Reply to remote user
                    plain = renderable.plain.strip()
                    if plain and msg_obj is not None:
                        try:
                            await msg_obj.reply_fn(plain)
                        except Exception:
                            pass

                from novacode_cli.commands.ralph_handler import handle_ralph_command

                parts = text.split(maxsplit=1)
                ralph_args = parts[1].strip() if len(parts) > 1 else ""

                await handle_ralph_command(
                    self.agent,
                    self.session_state,
                    self.assistant_id,
                    self.token_tracker,
                    ralph_args or None,
                    execute_fn=self._tui_execute_fn,
                    emit=_emit_remote,
                )

            return None, run_ralph

        if cmd in self._REMOTE_LOCAL_ONLY:
            return f"/{cmd} is only available in the local TUI.", None

        # Otherwise treat it as a skill: /skill:<name> or a bare /<name>.
        skill_name = cmd[len("skill:") :] if cmd.startswith("skill:") else cmd
        if skill_name:
            try:
                from novacode_cli.commands.skill_invoke import _try_skill_invocation

                skill = await _try_skill_invocation(
                    skill_name, arg or None, self.session_state, self.assistant_id
                )
            except Exception as ex:  # noqa: BLE001
                return f"❌ /{cmd} failed: {ex}", None
            if skill is not None:
                return None, skill.pinned_prompt or skill.prompt

        return (
            f"Unknown command: /{cmd}. Send /help for what works over remote.",
            None,
        )

    async def _run_bash(self, text: str) -> None:
        """Run a ``!`` shell command.

        ``!cmd`` runs in the chat: output streams into a card and the prompt
        stays usable, so a long command never holds the UI (``/kill bg-<n>``
        stops it). ``!!cmd`` hands the real terminal to the command instead, by
        suspending the TUI, for anything that needs a keyboard (an editor, a
        REPL, a password prompt): a card has no stdin to give it.
        """
        cmd = text[1:].strip()
        if not cmd:
            return
        if not cmd.startswith("!"):
            self._bg_job_count += 1
            self._bg_shell_worker(cmd, self._bg_job_count, foreground=True)
            return
        cmd = cmd[1:].strip()
        if not cmd:
            return

        import sys
        import subprocess
        from novacode_cli.config.config import settings

        # Log the command in the transcript
        self._log(Text(f"Executing: !{cmd}", style="bold yellow"))

        # Suspend Textual and run the command directly on the system terminal
        from textual.app import SuspendNotSupported

        try:
            with self.suspend():
                cwd = settings.get_workspace_root()
                if sys.stdin.isatty():
                    print(f"\n--- Executing command in {cwd.name} ---")
                    print(f"> {cmd}\n")
                try:
                    # Off the loop: the child still inherits this terminal, but a
                    # synchronous run here freezes the agent stream, the status
                    # tick and the stall watchdog for the whole command.
                    res = await asyncio.to_thread(subprocess.run, cmd, shell=True, cwd=cwd)
                    exit_code = res.returncode
                except Exception as ex:  # noqa: BLE001
                    exit_code = -1
                    print(f"Error executing command: {ex}")

                if sys.stdin.isatty():
                    print("\n--- Command finished. Press Enter to return to TUI ---")
                    try:
                        await asyncio.to_thread(input)
                    except (KeyboardInterrupt, EOFError):
                        pass
        except SuspendNotSupported:
            # Fallback for non-interactive/headless test environments where suspend is not supported
            cwd = settings.get_workspace_root()
            try:
                # Same reason as the suspend path, and this is the branch every
                # headless/`run_test()` environment takes, so a synchronous run
                # put a blocking subprocess on the loop in every such test.
                res = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    shell=True,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                exit_code = res.returncode
            except Exception:  # noqa: BLE001
                exit_code = -1

        if exit_code == 0:
            self._log(Text("● Command finished successfully.", style="green"))
        else:
            self._log(Text(f"❌ Command exited with code {exit_code}.", style="red"))

    # -- background shell (ctrl+b) --------------------------------------------

    @work(group="bgshell")
    async def _bg_shell_worker(self, cmd: str, job_id: int, *, foreground: bool = False) -> None:
        """Run *cmd* as a non-blocking background subprocess.

        Each call spawns an independent worker in group ``"bgshell"`` (no
        ``exclusive=True``) so multiple Ctrl+B jobs run in true parallel.
        Output streams line-by-line into a ``OutputLog`` inside a ``Collapsible``
        card.  When the process exits the card title flips to ●/✖ and the
        process is deregistered from ProcessManager.

        ``foreground`` is a plain ``!cmd``: no card, just a ``! cmd`` row with
        the output indented under it. Either way it is registered as
        ``bg-<job_id>``.
        """
        import os

        from novacode_cli.config.config import settings
        from novacode_cli.process_manager import ProcessInfo, ProcessManager, ProcessStatus

        cwd = settings.get_workspace_root()
        short = cmd if len(cmd) <= 50 else cmd[:47] + "…"
        label = f"bg[{job_id}]: {short}"
        pal = self._palette
        background_job = None
        hint = Static(
            Text("Ctrl+B · run in background", style="dim"), classes="background-shell-hint"
        )
        hint.display = foreground

        # Build the widget up-front so output starts streaming immediately.
        if foreground:
            log_widget = OutputLog(
                classes="bash-inline-log",
                highlight=False,
                markup=False,
                wrap=True,
                max_lines=_LOG_MAX_LINES,
            )
            head = Static(classes="bash-inline-head")
            card: Any = Vertical(head, log_widget, hint, classes="bash-inline")
        else:
            log_widget = OutputLog(
                classes="bgshell-log", highlight=True, markup=True, max_lines=_LOG_MAX_LINES
            )
            card = Collapsible(Vertical(log_widget), title="", collapsed=False)
            card.add_class("bgshell-card")

        def set_state(mark: str, state: str, *, bad: bool = False) -> None:
            """Show where the command is: the card's title, or the inline row."""
            if not foreground:
                card.title = f"{mark} {label}  [{state}]"
                return
            row = Text()
            row.append("! ", style=f"bold {pal.accent}")
            row.append(cmd)
            if state == "running":
                row.append(f"   running · /kill bg-{job_id}", style="dim")
            elif state.startswith("background"):
                row.append(f"   {state}", style=f"bold {pal.accent}")
            elif bad:
                row.append(f"   {state}", style=f"bold {pal.error}")
            _paint(head, row)

        emitted = 0
        from collections import deque

        from novacode_cli.session.transcript_journal import MAX_LINES

        captured: deque[str] = deque(maxlen=MAX_LINES)  # for the session journal

        def emit(line: str, *, style: str = "") -> None:
            nonlocal emitted
            if not style:  # command output, not our own "(no output)" / error notes
                captured.append(line)
            if not foreground:
                log_widget.write(line)
            else:
                row = Text("  └  " if not emitted else "     ", style="dim")
                row.append(line, style=style)
                log_widget.write(row)
            emitted += 1

        def emit_batch(lines: list[str]) -> None:
            nonlocal emitted
            captured.extend(line[:2000] for line in lines)
            if background_job is not None:
                shell_jobs.get_registry().append_log(background_job.id, "\n".join(lines) + "\n")
            if foreground:
                row = Text()
                for index, line in enumerate(lines):
                    if index:
                        row.append("\n")
                    row.append("  └  " if not emitted and not index else "     ", style="dim")
                    row.append(line)
                log_widget.write(row)
            else:
                log_widget.write("\n".join(lines))
            emitted += len(lines)

        set_state("◐", "running")
        self._close_tool_group()
        await self._transcript().mount(card)
        self._prune_transcript()
        self._scroll_end()

        # Spawn the subprocess with merged stdout+stderr so the log shows both.
        try:
            import subprocess

            group_options = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # No stdin: an inherited one lets the child read the keystrokes
                # meant for the TUI, and a command that prompts would hang
                # unseen. It gets EOF instead; `!!cmd` is the interactive path.
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(cwd),
                env=os.environ.copy(),
                **group_options,
            )
        except Exception as ex:  # noqa: BLE001
            hint.display = False
            emit(f"Failed to start: {ex}", style="bold red") if foreground else log_widget.write(
                f"[bold red]Failed to start: {ex}[/bold red]"
            )
            set_state("✖", "failed to start", bad=True)
            return

        # Register with ProcessManager so `/kill bg-<n>` or `/kill <pid>` works.
        info = ProcessInfo(
            pid=process.pid,
            name=f"bg-{job_id}",
            command=cmd,
            status=ProcessStatus.RUNNING,
            working_dir=str(cwd),
            _process=process,
        )
        ProcessManager.get_instance().register_process(info)

        from novacode_cli.shell import jobs as shell_jobs
        from novacode_cli.shell.middleware import ShellMiddleware

        control = shell_jobs.set_current(cmd) if foreground else None

        def background_command() -> None:
            nonlocal background_job
            background_job = shell_jobs.get_registry().add(cmd, "shell", None)
            shell_jobs.get_registry().attach_pid(background_job.id, process.pid)
            background_job.logs.extend(line + "\n" for line in captured)
            if control is not None:
                shell_jobs.clear_current(control)
            hint.display = False
            set_state("●", f"background · {background_job.task_id}")

        async def monitor_controls() -> None:
            while process.returncode is None:
                if control is not None and control.detach.is_set() and background_job is None:
                    background_command()
                if (control is not None and control.kill.is_set()) or (
                    background_job is not None and background_job.kill.is_set()
                ):
                    await ShellMiddleware._terminate_tree(process, grace=0)
                    return
                await asyncio.sleep(0.1)

        if not foreground:
            background_command()
        monitor = asyncio.create_task(monitor_controls())

        # Batch ready output and yield between reads so input stays responsive.
        assert process.stdout is not None  # noqa: S101  — PIPE guarantees this
        try:
            from novacode_cli.tui.shell_output import shell_output_batches

            async for lines in shell_output_batches(process.stdout):
                emit_batch(lines)
            await process.wait()
        except asyncio.CancelledError:
            await asyncio.shield(ShellMiddleware._terminate_tree(process, grace=0))
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=2.0)
            if process.returncode is None:
                with contextlib.suppress(Exception):
                    process.kill()
                    await process.wait()
            set_state("✖", "cancelled", bad=True)
            self._journal(
                {
                    "k": "bash",
                    "cmd": cmd,
                    "lines": list(captured),
                    "exit": None,
                    "fg": foreground,
                    "job": job_id,
                }
            )
            info.status = ProcessStatus.STOPPED
            ProcessManager.get_instance().unregister_process(info.pid)
            if background_job is not None:
                shell_jobs.get_registry().mark_terminated(background_job.id, process.returncode)
            return
        finally:
            hint.display = False
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
            if control is not None:
                shell_jobs.clear_current(control)

        ProcessManager.get_instance().unregister_process(info.pid)
        exit_code = process.returncode or 0
        if background_job is not None:
            registry = shell_jobs.get_registry()
            if background_job.kill.is_set():
                registry.mark_terminated(background_job.id, exit_code)
            else:
                registry.complete(background_job.id, exit_code)
        if not emitted:
            emit("(no output)", style="dim") if foreground else log_widget.write(
                "[dim](no output)[/dim]"
            )

        # Update the title/row and ProcessManager status.
        if exit_code == 0:
            set_state("●", "exit 0")
            card.add_class("bgshell-done")
            info.status = ProcessStatus.STOPPED
        else:
            set_state("✖", f"exit {exit_code}", bad=True)
            card.add_class("bgshell-failed")
            info.status = ProcessStatus.FAILED
            if not foreground:
                log_widget.write(f"[bold red]Exited with code {exit_code}[/bold red]")

        # Collapse finished background cards so they don't crowd the transcript.
        if not foreground:
            card.collapsed = True
        self._journal(
            {
                "k": "bash",
                "cmd": cmd,
                "lines": list(captured),
                "exit": exit_code,
                "fg": foreground,
                "job": job_id,
            }
        )

    # -- background agent turn (ctrl+b, non-! input) --------------------------

    @work(group="bgagent")
    async def _bg_agent_worker(  # noqa: PLR0915 — linear stream-handling loop
        self, prompt: str, job_id: int
    ) -> None:
        """Run a full agent turn in the background without blocking the main input.

        Uses a fresh thread_id so the background conversation is isolated from the
        main session. All tool approvals are auto-approved (the user opted in by
        pressing Ctrl+B), so the turn never blocks waiting for a decision.
        """
        import uuid

        from novacode_cli.agent_stream import run_agent_stream
        from novacode_cli.remote.status import feed as feed_remote_status
        from novacode_cli.ui_events import (
            AssistantMessage,
            Done,
            Error,
            InterruptRequest,
        )

        thread_id = f"bg-{uuid.uuid4().hex[:12]}"
        task = self._bg_agent_tasks.get(f"agent-{job_id}")
        if task is None:
            from novacode_cli.tui.background_tasks import AgentTask

            task = AgentTask(f"agent-{job_id}", prompt)
            self._bg_agent_tasks[task.task_id] = task
            from textual.worker import get_current_worker

            task.worker = get_current_worker()
        self._refresh_tasks_bar()
        p_short = prompt if len(prompt) <= 50 else prompt[:47] + "…"

        # Minimal proxy session state — only the fields iterate_agent_events reads.
        # auto_approve=True makes evaluate_tool_actions return allow for every tool
        # and auto-resolves plan interrupts, so no InterruptRequest is yielded for
        # either. Only ask_user_question (kind="question") can still interrupt; we
        # resolve those below with a canned answer.
        class _BgSession:
            def __init__(self_inner, real_ss: Any) -> None:  # noqa: N805
                self_inner.thread_id = thread_id
                self_inner.auto_approve = True
                self_inner.plan_mode_enabled = False
                self_inner.plan_agent = None
                self_inner.plan_content: Any = None
                self_inner.active_goal: str | None = getattr(real_ss, "active_goal", None)

            def add_notification(self_inner, **_kw: Any) -> None:  # noqa: N805
                return None

            def dismiss_notification(self_inner, _nid: Any) -> None:  # noqa: N805
                pass

            def register_pending_approval(self_inner, _iid: Any, _fut: Any) -> None:  # noqa: N805
                pass

            def set_approved_plan(self_inner, _plan: Any) -> None:  # noqa: N805
                pass

            def clear_plan_agent(self_inner) -> None:  # noqa: N805
                pass

        bg_session = _BgSession(self.session_state)
        ag, backend = self._active_agent()

        log_widget = OutputLog(
            classes="bgagent-log", highlight=True, markup=True, max_lines=_LOG_MAX_LINES
        )
        card = Collapsible(
            Vertical(log_widget),
            title=f"⟳ bg[{job_id}] · {p_short}",
            collapsed=False,
        )
        card.add_class("bgagent-card")
        self._close_tool_group()
        await self._transcript().mount(card)
        self._prune_transcript()
        self._scroll_end()

        # Live "what is happening" line, mirrored into the card title so it is
        # visible even when the card is collapsed. The body keeps the full trace.
        def _set_phase(phase: str) -> None:
            card.title = f"⟳ bg[{job_id}] · {p_short}  ·  {phase}"

        def _write(markup: str) -> None:
            log_widget.write(markup)
            if task is not None:
                task.logs.append(markup[:2000])

        # In-flight tool calls, buffered so a call and its result render as one
        # line (OutputLog cannot rewrite a line once written).
        pending: dict[str, str] = {}

        # The final answer, captured so the main agent can be told what the
        # background run concluded (not just that it finished).
        final_text: list[str] = []

        remote_streams = []
        try:
            remote_streams = await self._create_local_remote_streams(prompt, label=f"bg[{job_id}]")
            async for e in run_agent_stream(
                prompt,
                ag,
                self.assistant_id,
                bg_session,
                backend=backend,
                seen_message_ids=set(),
            ):
                for _target, remote_status, remote_answer in remote_streams:
                    feed_remote_status(remote_status, e)
                    remote_answer.feed(e)
                if isinstance(e, InterruptRequest):
                    # Only ask_user_question reaches here (tools and plans are
                    # auto-approved via auto_approve=True on the session).
                    # Provide a canned answer so the turn continues unblocked.
                    try:
                        if e.kind == "question":
                            e.future.set_result({"answer": "Please continue autonomously."})
                        else:
                            from novacode_cli.core.agent_loop import default_interrupt_response

                            e.future.set_result(default_interrupt_response(e.kind))
                    except Exception:  # noqa: BLE001
                        pass
                elif isinstance(e, Error):
                    if task is not None:
                        task.finish("failed")
                    _write(f"Error: {e.message}")
                    break
                elif isinstance(e, Done):
                    break
                else:
                    # Everything else is a progress event: render it into the card
                    # body and update the live phase line.
                    if isinstance(e, AssistantMessage) and e.text:
                        final_text.append(e.text)
                    _render_bg_event(e, _write, _set_phase, self._fileop_summary, pending)
        except asyncio.CancelledError:
            for _target, _status, answer in remote_streams:
                answer.cancelled = True
            if task is not None:
                task.finish("terminated")
            self._refresh_tasks_bar()
            card.title = f"✖ bg[{job_id}] · {p_short}  ·  cancelled"
            return
        except Exception as ex:  # noqa: BLE001
            for _target, _status, answer in remote_streams:
                answer.error = str(ex)
            if task is not None:
                task.finish("failed")
            self._refresh_tasks_bar()
            _write(f"[bold red]Error: {ex}[/bold red]")
            card.title = f"✖ bg[{job_id}] · {p_short}  ·  error"
            card.add_class("bgagent-failed")
            card.collapsed = True
            self._journal(
                {
                    "k": "bgagent",
                    "prompt": prompt,
                    "status": "error",
                    "summary": str(ex),
                    "job": job_id,
                }
            )
            return
        finally:
            await self._finish_remote_streams(remote_streams)

        # A tool call whose result never arrived (turn ended mid-call) still needs
        # to appear, or the trace would silently drop it.
        for prefix in pending.values():
            _write(prefix)
        pending.clear()

        if task is not None:
            task.finish("done")
        self._refresh_tasks_bar()
        status = task.status if task is not None else "done"
        card.title = f"● bg[{job_id}] · {p_short}  ·  {status}"
        card.add_class("bgagent-failed" if status == "failed" else "bgagent-done")
        card.collapsed = True
        self._journal(
            {
                "k": "bgagent",
                "prompt": prompt,
                "status": status,
                "summary": "\n".join(final_text)[-4000:],
                "job": job_id,
            }
        )

        # Report the outcome back to the main agent so it can summarise and act on
        # what the background run found, rather than the user having to relay it.
        if status == "done":
            self._report_bg_agent_done(job_id, prompt, "\n".join(final_text))

    def _report_bg_agent_done(self, job_id: int, prompt: str, final_text: str) -> None:
        """Queue a note telling the main agent what a background run concluded.

        The note is prepended to the agent's next turn (see the
        ``_pending_job_notes`` drain in the submit path), so the agent can
        summarise the result and act on it without the user relaying it by hand.

        Args:
            job_id: The background job number (``bg[<job_id>]``).
            prompt: The task the background run was given.
            final_text: The run's final assistant prose (may be empty).
        """
        summary = final_text.strip()
        if len(summary) > _BG_REPORT_MAX_CHARS:
            summary = summary[:_BG_REPORT_MAX_CHARS] + "\n… (truncated)"
        note = f"Background agent bg[{job_id}] finished. Task: {prompt}" + (
            f"\n\nIts final answer:\n{summary}" if summary else "\n\n(no final answer)"
        )
        self._queue_pending_job_note(note)
        root = getattr(self, "_root_pane", None)
        if root is not None and root is not getattr(self, "_active_pane", None):
            self._queue_pane_event(
                root,
                ev.ContextMessage(
                    message=f"Background agent bg[{job_id}] finished.", color="green"
                ),
            )
            root.unread += 1
            return
        self._log(
            Text(
                f"● Background agent bg[{job_id}] finished — the agent will be told "
                f"on its next turn.",
                style="green",
            )
        )

    def _queue_pending_job_note(self, note: str) -> None:
        """Keep unattended task notes useful without letting them grow forever."""
        root = getattr(self, "_root_pane", None)
        notes = (
            root.state.setdefault("_pending_job_notes", [])
            if root is not None and root is not getattr(self, "_active_pane", None)
            else self._pending_job_notes
        )
        notes.append(note)
        overflow = len(notes) - _PENDING_JOB_NOTE_MAX
        if overflow > 0:
            del notes[:overflow]

    async def _run_slash(self, text: str) -> None:
        """Handle the TUI-native slash command subset — table dispatch.

        Lookup order: /skill:<name> prefix → TUI_COMMANDS table (with aliases)
        → plugin-contributed command → bare /<name> skill → unknown notice.
        """
        import inspect

        cmd = text[1:].split(maxsplit=1)[0].lower() if len(text) > 1 else ""
        if cmd == "ui":
            await self._run_ui(text)
            return
        if cmd in self._ui_harness.applied.commands:
            await self._ui_harness.toggle(self._ui_harness.applied.commands[cmd])
            return
        if cmd.startswith("skill:"):
            # /skill:<name> — resolve + render natively, then stream the prompt.
            await self._run_skill(text)
            return

        spec = TUI_COMMANDS.get(cmd) or TUI_COMMANDS.get(_TUI_COMMAND_ALIASES.get(cmd, ""))
        if spec is not None:
            handler = getattr(self, spec.handler)
            result = handler(text) if spec.wants_text else handler()
            if inspect.isawaitable(result):
                await result
            return

        # A slash command contributed by an enabled plugin.
        if await self._run_plugin_command(text):
            return
        # A bare /<name> may be a skill (e.g. /graphify) — resolve it natively
        # before reporting the command as unavailable.
        if await self._run_skill(text):
            return
        self._log(
            Text(
                f"/{cmd} isn't a recognized command. Type /help to list commands.",
                style="yellow",
            )
        )

    async def _run_ui(self, text: str) -> None:
        parts = text.split(maxsplit=2)
        operation = parts[1].lower() if len(parts) > 1 else "inspect"
        try:
            payload = json.loads(parts[2]) if len(parts) > 2 else {}
            if not isinstance(payload, dict):
                self._log(Text("UI patches must be JSON objects.", style="yellow"))
                return
            result = await self._ui_harness.execute(operation, payload)
            self._log(Text(json.dumps(result, indent=2), style="dim"))
        except (ValueError, OSError) as error:
            self._log(Text(f"UI change failed: {error}", style="yellow"))

    async def action_ui_reset(self) -> None:
        """Restore the default layout through the reserved recovery shortcut."""
        await self._run_ui("/ui reset")

    async def action_harness_key(self, key: str) -> None:
        """Handle the optional panel shortcuts without replacing core bindings."""
        if action := self._ui_harness.applied.bindings.get(key):
            await self._ui_harness.toggle(action)

    async def on_uiharness_request(self, message: UIHarnessRequest) -> None:
        """Serialize agent UI operations and reply on the caller's event loop."""
        if message.future.done():
            return
        try:
            result = await self._ui_harness.execute(message.operation, message.payload)
        except Exception as error:
            result = {"error": str(error)}

        def finish() -> None:
            if not message.future.done():
                message.future.set_result(result)

        message.future.get_loop().call_soon_threadsafe(finish)

    # ── Small handlers extracted from the old _run_slash elif chain ────────
    # (inline blocks became methods so every command fits the table contract)

    def _run_help(self) -> None:
        from novacode_cli.tui.reference_screens import HelpScreen

        self.push_screen(
            HelpScreen(
                TUI_COMMANDS,
                self._plugin_commands,
                self._ui_harness.applied.commands,
                skill_loader=self._get_skill_names,
            )
        )

    async def _run_remote(self, text: str) -> None:
        from novacode_cli.commands.commands import _handle_remote_command

        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await self._run_remote_screen()
            return
        with _rich_console.capture() as cap:
            await _handle_remote_command(parts[1], self._remote_owner_state(), _rich_console)
        if output := cap.get().strip():
            self._log(Text.from_ansi(output))
        self._ensure_remote_consumer()

    async def _run_remote_screen(self) -> None:
        await self.push_screen_wait(
            RemoteScreen(
                self._remote_owner_state(),
                sandbox_id=self._sandbox_id,
                sandbox_type=self._sandbox_type,
            )
        )
        self._ensure_remote_consumer()
        await self._sync_remote_topics()

    def _ensure_remote_consumer(self) -> None:
        """Starting /remote after launch must start exactly one message consumer."""
        if getattr(self._remote_owner_state(), "_remote_message_queue", None) is None:
            return
        manager = getattr(self._remote_owner_state(), "_remote_bridge_manager", None)
        if manager is not None:

            async def status_callback(message: str) -> None:
                self._log(Text(f"🔗 Remote: {message}", style="dim"))

            manager.set_status_callback(status_callback)
            self._remote_status_callback = status_callback
        worker = self._remote_consumer_worker
        if worker is None or worker.is_finished:
            self._remote_consumer_worker = self._remote_consumer()
        topic_worker = getattr(self, "_remote_topics_worker", None)
        if topic_worker is None or topic_worker.is_finished:
            self._remote_topics_worker = self.run_worker(
                self._sync_remote_topics(), group="remote_topics", exclusive=False
            )

    async def _start_local_remote_stream(self, prompt: str) -> None:
        self._local_remote_streams.extend(await self._create_local_remote_streams(prompt))

    async def _create_local_remote_streams(self, prompt: str, *, label: str = "main") -> list:
        from novacode_cli.remote.status import RemoteStatusLine
        from novacode_cli.remote.streaming import RemoteAnswerStream

        manager = getattr(self._remote_owner_state(), "_remote_bridge_manager", None)
        if manager is None:
            return []
        await self._sync_remote_topics()
        streams = []
        targets = await manager.stream_targets(sid="root")
        for target in targets:
            status = RemoteStatusLine(
                target.edit_fn, label=f"NOVA · {label} · " + " ".join(prompt.split())[:80]
            )
            answer = RemoteAnswerStream(target.answer_edit_fn)
            streams.append((target, status, answer))
            status.start()
            answer.start()
        return streams

    async def _finish_local_remote_stream(self) -> None:
        streams, self._local_remote_streams = self._local_remote_streams, []
        await self._finish_remote_streams(streams)

    async def _finish_remote_streams(self, streams: list) -> None:
        async def finish(target, status, answer):
            await status.finalize(
                outcome="failed" if answer.error else "stopped" if answer.cancelled else "done"
            )
            if not await answer.finalize():
                try:
                    await asyncio.wait_for(
                        target.reply_fn(answer.text or "Task finished; live delivery failed."),
                        timeout=10,
                    )
                except Exception:
                    self.notify(
                        "Remote response delivery failed. Check /remote status.", severity="warning"
                    )

        if streams:
            await asyncio.gather(*(finish(*stream) for stream in streams), return_exceptions=True)

    async def _run_theme(self) -> None:
        # Fire-and-forget: the theme screen's result is unused, and awaiting it
        # with push_screen_wait would hold the exclusive "turn" worker open for
        # as long as the modal is on screen, blocking every later command.
        self.push_screen(ThemeScreen())

    def _run_settings(self) -> None:
        """Open app preferences without holding the turn worker open."""
        self.push_screen(SettingsScreen())

    def _load_ui_preferences(self) -> None:
        """Load persistent input and performance preferences at startup."""
        import os

        from novacode_cli.config.nova_config import NovaConfig

        config = NovaConfig()
        self._submit_on_enter = config.get("submit_on_enter", True) is True
        self._autocomplete_enabled = config.get("autocomplete_enabled", True) is True
        self._low_resource_mode = config.get("low_resource_mode", False) is True
        from novacode_cli.tui.animation_rate import animation_fps

        self._animation_fps = animation_fps(config.get("animation_fps"))
        saved_rain = config.get("matrix_rain_enabled")
        self._animate_matrix_rain = (
            saved_rain
            if isinstance(saved_rain, bool)
            else os.environ.get("NOVA_ANIMATIONS", "").strip().lower() in {"1", "true", "yes"}
        )

    def _matrix_rain_enabled(self) -> bool:
        """Return the loaded Matrix Rain preference."""
        return self._animate_matrix_rain

    def _set_animation_fps(self, fps: object) -> None:
        """Apply a saved animation rate immediately without duplicating timers."""
        from novacode_cli.tui.animation_rate import animation_fps

        self._animation_fps = animation_fps(fps)
        self._schedule_status_tick()
        rain_fps = min(15, self._animation_fps) if self._low_resource_mode else self._animation_fps
        for rain in self.query(MatrixRain):
            rain.set_frame_rate(rain_fps)

    def _set_matrix_rain_enabled(self, enabled: bool) -> None:
        """Apply a Matrix Rain preference immediately to the visible banner."""
        self._animate_matrix_rain = enabled
        rain = self._home_banner
        if isinstance(rain, MatrixRain):
            rain.set_animation_enabled(enabled)

    def _run_token_view(self) -> None:
        self._log(self._token_text())

    async def _run_context(self, text: str = "/context") -> None:
        """Open the live context dashboard or retain imported-context controls."""
        if len(text.split()) > 1:
            await self._run_session_import(text)
            return
        await self._update_context_breakdown()
        self.push_screen(ContextScreen(self.token_tracker, self.model_name))

    def _run_verbose(self) -> None:
        new = self.session_state.toggle_verbose()
        self._log(
            Text(
                f"Verbose mode {'on' if new else 'off'} — internal context "
                f"{'shown' if new else 'collapsed'}.",
                style="green" if new else "dim",
            )
        )

    async def _run_ralph_screen(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        await self.push_screen_wait(
            RalphScreen(
                session_state=self.session_state,
                agent=self.agent,
                assistant_id=self.assistant_id,
                token_tracker=self.token_tracker,
                args=args,
                execute_fn=self._tui_execute_fn,
            )
        )

    async def _run_middleware(self) -> None:
        await self.push_screen_wait(PluginsScreen())
        # Reload plugin commands so newly-enabled command plugins work this
        # session (middleware/subagents still need a restart, as noted).
        self._load_plugin_commands()

    async def _run_plugins(self, text: str) -> None:
        """Native ``/plugins`` — Claude-compatible plugin installer + marketplaces.

        Mirrors the console handler but logs via ``self._log``. Reuses the shared
        install/marketplace logic in ``plugins.claude_plugins`` / ``.marketplaces``.
        """
        import re as _re

        from novacode_cli.plugins import claude_plugins as cp
        from novacode_cli.plugins import marketplaces as mp

        def log(msg: str, style: str = "") -> None:
            self._log(Text(msg, style=style) if style else Text(msg))

        def fmt(comps: dict) -> str:
            parts = [
                f"{k}: {', '.join(v)}"
                for k in ("skills", "commands", "agents", "mcp", "hooks")
                if (v := comps.get(k))
            ]
            return "  " + " | ".join(parts) if parts else "  (no components)"

        parts = text.split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else "list"
        arg = parts[2].strip() if len(parts) > 2 else ""
        try:
            if sub == "install":
                if not arg:
                    log(
                        "Usage: /plugins install <owner/repo | git-url | dir | plugin@marketplace>",
                        "yellow",
                    )
                    return
                name = (
                    mp.install_plugin(arg)
                    if _re.fullmatch(r"[\w.-]+@[\w.-]+", arg)
                    else cp.install(arg)
                )
                log(f"● Installed {name}", "green")
                log(fmt(cp.plugin_components(name)), "cyan")
                log("Restart Nova to activate.", "dim")
            elif sub == "remove":
                log(
                    f"● Removed {arg}" if cp.remove(arg) else f"No plugin named '{arg}'",
                    "green" if arg else "yellow",
                )
            elif sub == "search":
                hits = mp.list_marketplace_plugins()
                if arg:
                    q = arg.lower()
                    hits = [
                        p for p in hits if q in p["name"].lower() or q in p["description"].lower()
                    ]
                if not hits:
                    log(
                        "No matching plugins. Add a marketplace: /plugins marketplace add <owner/repo>",
                        "dim",
                    )
                else:
                    log("Available plugins:", "bold")
                    for p in hits:
                        log(f"  • {p['name']}@{p['marketplace']}  {p['description']}")
            elif sub == "marketplace":
                msub, _, marg = arg.partition(" ")
                marg = marg.strip()
                if msub == "add":
                    name = mp.add(marg)
                    log(
                        f"● Added marketplace {name} ({len(mp.list_marketplace_plugins())} plugins — /plugins search)",
                        "green",
                    )
                elif msub == "remove":
                    log(
                        f"● Removed marketplace {marg}"
                        if mp.remove_marketplace(marg)
                        else f"No marketplace '{marg}'",
                        "green" if marg else "yellow",
                    )
                else:
                    mkts = mp.list_marketplaces()
                    log(
                        "Marketplaces:"
                        if mkts
                        else "No marketplaces. /plugins marketplace add <owner/repo>",
                        "bold" if mkts else "dim",
                    )
                    for m in mkts:
                        log(f"  • {m['name']}  {m['source']}")
            else:  # list — open the native plugins viewer instead of a text dump
                await self.push_screen_wait(ClaudePluginsScreen())
        except (ValueError, RuntimeError) as e:
            log(f"Failed: {e}", "red")

    async def _run_reload_plugins(self) -> None:
        """``/reload-plugins`` — rebuild the agent to pick up plugins added/changed
        this session (skills, subagents, MCP), reload hooks, and refresh autocomplete.

        Reuses the agent-rebuild path (``reload_mcp_servers``): a fresh
        ``create_agent_with_config`` re-scans plugin skills/agents/MCP.
        """
        self._set_status(status_phrases.status_line("working"))
        try:
            # The agent-rebuild path prints build/skill/MCP chatter to the global
            # Rich console, which bypasses Textual and corrupts the TUI screen.
            # Capture it (like the other TUI handlers) — our own summary is logged
            # below via self._log.
            with _rich_console.capture():
                new_agent, new_backend = await self.session_state.reload_mcp_servers()
            self.agent = new_agent
            self.backend = new_backend
        except Exception as e:  # noqa: BLE001
            self._log(Text(f"Reload failed: {e}", style="red"))
            self._set_status("ready")
            return

        from novacode_cli import hooks as _hooks
        from novacode_cli.plugins import claude_plugins as cp

        _hooks.reload_hooks()  # plugin hooks re-read on next dispatch
        self._skill_names_cache = None  # refresh skill autocomplete
        self._load_plugin_commands()  # entry-point + Claude plugin commands

        n = len(cp.list_plugins())
        self._log(
            Text(
                f"● Reloaded {n} plugin(s) — skills, subagents, MCP, hooks, commands refreshed.",
                style="green",
            )
        )
        self._set_status("ready")

    def _load_plugin_commands(self) -> None:
        """Discover slash commands from enabled plugins and register them.

        Discovery scans every installed distribution's metadata and reads each
        plugin command file, which froze the UI for 2-3 s at start-up on a busy
        disk (freeze.log), so it runs on a thread and only the registration
        happens on the UI thread.
        """

        def work() -> None:
            found = self._discover_plugin_commands()
            try:
                if self.is_running:
                    self.call_from_thread(self._register_plugin_commands, found)
            except Exception:  # noqa: BLE001 — the app closed while we were scanning
                pass

        threading.Thread(target=work, name="nova-plugin-commands", daemon=True).start()

    def _register_plugin_commands(self, found: dict[str, Any]) -> None:
        self._plugin_commands = found
        for name in found:
            slash = f"/{name}"
            if slash not in _TUI_SLASH_COMMANDS:
                _TUI_SLASH_COMMANDS.append(slash)

    def _discover_plugin_commands(self) -> dict[str, Any]:
        """Name → async handler for every enabled plugin's commands. Thread-safe.

        Built-ins are matched first in :meth:`_run_slash`, so a plugin can't
        shadow a core command.
        """
        found: dict[str, Any] = {}
        try:
            from novacode_cli.plugins.loader import (
                collect_plugin_commands,
                discover_enabled_plugins,
            )

            cmds = collect_plugin_commands(discover_enabled_plugins())  # type: ignore
            found = {name: c["handler"] for name, c in cmds.items() if c.get("handler")}
        except Exception:  # noqa: BLE001 — a bad plugin must not break startup
            found = {}

        # Claude-compatible plugin commands (commands/*.md|*.toml). Invoking one
        # streams its body — with $ARGUMENTS / {{args}} substituted — to the agent
        # as a prompt (mirrors the console's _register_claude_plugin_commands).
        # Entry-point handlers above win on a name collision (setdefault).
        try:
            from novacode_cli.plugins.claude_plugins import plugin_commands

            def _make_claude_handler(body: str):
                async def _handler(args: str) -> str:
                    a = (args or "").strip()
                    prompt = body.replace("$ARGUMENTS", a).replace("{{args}}", a)
                    await self._stream_prompt(prompt)
                    return ""

                return _handler

            for cname, _desc, body in plugin_commands():
                found.setdefault(cname, _make_claude_handler(body))
        except Exception:  # noqa: BLE001 — a bad plugin must not break startup
            pass
        return found

    async def _run_plugin_command(self, text: str) -> bool:
        """Dispatch a plugin-contributed slash command. Returns True if handled.

        Built-ins are matched earlier in :meth:`_run_slash`, so they always win.
        The plugin handler is ``async (args) -> str``; its returned text is logged.
        """
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        handler = self._plugin_commands.get(cmd)
        if handler is None:
            return False
        try:
            result = await handler(args)
            if result:
                self._log(Text(str(result)))
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Plugin command /{cmd} failed: {ex}", style="red"))
        return True

    async def _run_copy(self, text: str) -> None:
        """Copy agent output to the clipboard.

        ``/copy``      — copy the last Nova response.
        ``/copy all``  — copy the whole conversation (You/Nova turns).
        """
        parts = text[1:].split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        msgs = list(self._transcript().query(ChatMessage))
        if not msgs:
            self._log(Text("Nothing to copy yet.", style="dim"))
            return

        if arg == "all":
            blocks: list[str] = []
            for m in msgs:
                body = (m.raw_text or "").strip()
                if not body:
                    continue
                who = "You" if m.has_class("user") else "Nova"
                blocks.append(f"## {who}\n{body}")
            payload = "\n\n".join(blocks)
            label = "conversation"
        else:
            nova = [m for m in msgs if m.has_class("nova")]
            if not nova:
                self._log(Text("No agent response to copy yet.", style="dim"))
                return
            payload = (nova[-1].raw_text or "").strip()
            label = "last response"

        if not payload:
            self._log(Text("Nothing to copy.", style="dim"))
            return
        try:
            self.copy_to_clipboard(payload)
            self._log(
                Text(
                    f"📋 Copied {label} ({len(payload):,} chars) to clipboard",
                    style="dim",
                )
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Copy failed: {ex}", style="red"))

    async def _run_skill(self, text: str) -> bool:
        """Resolve a ``/skill:<name>`` (or bare ``/<name>``) and run it natively.

        Renders the "⚡ Invoking skill" block as native widgets (the resolver is
        presentation-free) and streams the skill prompt. Returns ``True`` when a
        skill matched, ``False`` otherwise so the caller can fall back to the
        "command unavailable" notice.
        """
        raw = text[1:]
        if raw.lower().startswith("skill:"):
            raw = raw[len("skill:") :]
        parts = raw.split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else None
        if not name:
            self._log(Text("Usage: /<skill-name> [args]", style="yellow"))
            return True

        from novacode_cli.commands.skill_invoke import _try_skill_invocation

        try:
            skill = await _try_skill_invocation(name, args, self.session_state, self.assistant_id)
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/skill:{name} failed: {ex}", style="red"))
            return True
        if skill is None:
            return False

        t = Text()
        if skill.pinned_prompt:
            label = self._user_label()
            label.append(f" · pinned skill: {skill.name}", style="bold #7aa2f7")
            await self._add_message(label, "user", Text(text))
        t.append(
            f"⚡ {'Pinning' if skill.pinned_prompt else 'Invoking'} skill: {skill.name}",
            style="bold #7aa2f7",
        )
        if skill.description:
            t.append(f"\n  {skill.description}", style="dim")
        t.append(f"\n  Source: {skill.source}", style="dim")
        if skill.args:
            t.append(f"\n  Arguments: {skill.args}", style="dim")
        if skill.supporting_files:
            t.append(
                f"\n  Supporting files: {', '.join(skill.supporting_files)}",
                style="dim",
            )
        self._log(t)
        await self._stream_prompt(skill.pinned_prompt or skill.prompt)
        return True

    async def _passthrough_command(self, text: str) -> None:
        """Run a print/toggle-only legacy slash command and show its output.

        Captures the global console so the existing handler's ``console.print``
        calls render into the transcript instead of the real terminal.
        """
        from novacode_cli.commands.commands import handle_command

        try:
            with _rich_console.capture() as cap:
                result = await handle_command(
                    text,
                    self.agent,
                    self.token_tracker,
                    self.session_state,
                    self.assistant_id,
                    model_name=self.model_name,
                    image_tracker=self.image_tracker,
                    sandbox_id=self._sandbox_id,
                    sandbox_type=self._sandbox_type,
                )
            out = cap.get()
            if out.strip():
                self._log(Text.from_ansi(out))
            # Sync active TUI agent/backend in case model was switched dynamically.
            # SessionState exposes these as `_agent` / `_backend`; fall back to the
            # current values so a command that doesn't touch them can't blow up.
            if self.session_state is not None:
                self.agent = getattr(self.session_state, "_agent", self.agent)
                self.backend = getattr(self.session_state, "_backend", self.backend)
                model = getattr(self.session_state, "model", None)
                if model:
                    self.model_name = getattr(model, "model_name", None) or getattr(
                        model, "model", "unknown"
                    )
                    if self.token_tracker is not None:
                        try:
                            self.token_tracker.set_model(self.model_name)
                        except Exception:  # noqa: BLE001
                            pass
            # Some handlers return a prompt string to feed back to the agent.
            if isinstance(result, str):
                await self._stream_prompt(result)

            # Reset the voice pipeline on `/voice` settings change so it is re-initialized with the new config.
            if text.strip().lower().startswith(("/voice ", "/voice")):
                self._voice_pipeline = None
                if hasattr(self.session_state, "_voice_pipeline"):
                    self.session_state._voice_pipeline = None
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/{text[1:].split()[0]} failed: {ex}", style="red"))

    @work(group="voice_cmd", exclusive=True)
    async def _run_voice(self, text: str) -> None:
        """Run a ``/voice`` subcommand off the UI coroutine.

        ``/voice test`` and ``/voice download`` can take seconds (model load /
        synthesis / download). Running them in a worker keeps the TUI responsive
        instead of freezing on the captured-output passthrough. Fast subcommands
        (status, on/off, settings…) work here too — the worker just returns
        quickly. Output is captured the same way as ``_passthrough_command``.
        """
        from novacode_cli.commands.commands import handle_command

        try:
            with _rich_console.capture() as cap:
                await handle_command(
                    text,
                    self.agent,
                    self.token_tracker,
                    self.session_state,
                    self.assistant_id,
                    model_name=self.model_name,
                    image_tracker=self.image_tracker,
                    sandbox_id=self._sandbox_id,
                    sandbox_type=self._sandbox_type,
                )
            out = cap.get()
            if out.strip():
                self._log(Text.from_ansi(out))
            # A config-changing subcommand (on/off/mode/speak/settings) must
            # rebuild the cached pipeline so the next use picks up the new config.
            # test/download/status/doctor don't change config — leave the warmed
            # pipeline intact.
            sub = text.split(maxsplit=1)
            subcmd = sub[1].split()[0].lower() if len(sub) > 1 and sub[1].split() else ""
            if subcmd in ("on", "off", "mode", "speak", "settings"):
                self._voice_pipeline = None
                if hasattr(self.session_state, "_voice_pipeline"):
                    self.session_state._voice_pipeline = None
        except Exception as ex:  # noqa: BLE001 — a voice command must never crash the TUI
            self._log(Text(f"/voice failed: {ex}", style="red"))

    async def _run_model(self) -> None:
        """Native /model: pick a model from the provider-grouped list, hot-swap.

        The list carries two kinds of choice. A chat model hot-swaps the agent; a
        voice row (Deepgram, ElevenLabs, a local STT/TTS provider) only rewrites
        the voice config and rebuilds the cached pipeline. The row's ``kind``
        decides which, because a voice provider id is absent from
        ``MODEL_PRESETS`` and would raise there.

        A provider with no usable credential is not a dead end either way: the
        picker reports ``needs_auth``, the user is routed into ``/auth`` for that
        provider, and the picker reopens afterwards.
        """
        from novacode_cli.config.model_manager import MODEL_PRESETS, ModelManager

        mm = ModelManager()
        current_id = mm.get_current_provider_id()

        while True:
            result = await self.push_screen_wait(ModelScreen(current_id, self.model_name))
            if not result:
                return
            if result.get("needs_auth"):
                provider = str(result["provider"])
                await self._run_auth(provider)
                # Reopen with the same provider highlighted: the user's next
                # action is picking a model now that the key exists.
                current_id = provider
                continue
            break

        if result.get("kind") == "voice":
            self._apply_voice_pick(result)
            return

        if result.get("kind") == "decision":
            try:
                live_model = getattr(self.session_state, "_model", None)
                if live_model is None:
                    self._log(
                        Text("Decision settings saved; used in your next session.", style="yellow")
                    )
                    return
                self.agent, self.backend = await self.session_state.switch_model(live_model)
                state = "enabled" if result.get("enabled") else "disabled"
                self._log(
                    Text(f"System One tool pruning {state} ({result['model']}).", style="green")
                )
            except Exception:  # noqa: BLE001 — settings persist even if rebuilding fails
                self._log(
                    Text("Decision settings saved; restart Nova to apply them.", style="yellow")
                )
            return

        role = str(result.get("role") or "main")
        if role != "main":
            self._apply_role_pick(result, role)
            return

        provider = result["provider"]
        preset = MODEL_PRESETS[provider]
        model = result["model"] or preset["default_model"]

        # The endpoint travels with the credential, and `/auth` owns that field,
        # so it is read back here rather than re-entered in the picker.
        base_url = self._credential_base_url(provider)

        # Ensure the API key is present in the environment (model creation reads
        # os.environ). `resolve_api_key` exports a keychain/env key when one
        # exists; a custom endpoint (LM Studio, vLLM, a local proxy) usually
        # needs no key at all, so a URL alone is enough to proceed.
        if preset["requires_api_key"] and not mm.resolve_api_key(provider) and not base_url:
            self._log(
                Text(
                    f"{preset['name']} has no API key — run /auth to add one.",
                    style="red",
                )
            )
            return

        mm.set_provider(provider, model, base_url or None)
        try:
            from novacode_cli.config.model_create import create_model

            new_model = create_model()
            new_agent, new_backend = await self.session_state.switch_model(new_model)
            self.agent = new_agent
            self.backend = new_backend
            self.model_name = getattr(new_model, "model_name", None) or getattr(
                new_model, "model", "unknown"
            )
            # Keep the recorded provider in step with the live model, so the
            # next save records what the user actually switched to and a later
            # resume restores it rather than the pre-switch model.
            self._model_provider = provider
            if self.token_tracker is not None:
                try:
                    self.token_tracker.set_model(self.model_name)
                except Exception:  # noqa: BLE001
                    pass
            self._set_status("ready")
            await self._update_context_breakdown()
            self._refresh_info_bar()  # reflect the new model in the footer at once
            self._remember_model(provider, model)
            self._log(
                Text(
                    f"● Switched to {preset['name']} · {self.model_name}",
                    style="green",
                )
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Model switch failed: {ex}", style="red"))

    async def _run_router(self) -> None:
        """Native /router: configure per-turn model routing, then rebuild the agent.

        The screen owns the editing and dismisses a payload; this method owns the
        writing, matching the ``/model`` contract. Routing is a middleware, so a
        change only takes effect once the agent is rebuilt -- which is why the
        hot-swap below is not optional.
        """
        from novacode_cli.config.nova_config import NovaConfig

        config = NovaConfig()
        result = await self.push_screen_wait(RouterScreen(config))
        if not result:
            return

        try:
            new_profiles = result.get("new_profiles", {})
            for profile_id, profile in result["profiles"].items():
                if profile_id in new_profiles:
                    created_id = config.create_router_profile(new_profiles[profile_id])
                    if created_id != profile_id:
                        raise ValueError(f"Profile name maps to unexpected id {created_id!r}.")
                else:
                    config.set_active_router_profile(profile_id)
                config.set_router_routes(profile.get("routes", []))
                config.set_router_enabled(bool(profile.get("enabled", False)))
                config.set_router_default_route(profile.get("default_route"))
                config.set_router_decision_endpoint(
                    str(profile.get("decision_endpoint") or config.ROUTER_DEFAULT_ENDPOINT)
                )
                config.set_router_decision_model(
                    str(profile.get("decision_model") or config.ROUTER_DEFAULT_MODEL)
                )
            config.set_active_router_profile(result["profile_id"])
        except ValueError as ex:
            self._log(Text(f"Router settings not saved: {ex}", style="red"))
            return

        active_settings = result["profiles"][result["profile_id"]]
        self._router_mode_enabled = bool(active_settings.get("enabled"))
        self._routed_model_name = None
        self._refresh_info_bar()
        if not active_settings.get("enabled"):
            self._log(Text("Model routing disabled.", style="green"))
        else:
            self._log(
                Text(
                    f"Model routing enabled · {len(active_settings.get('routes', []))} route(s) · "
                    f"decided by {active_settings.get('decision_model', config.ROUTER_DEFAULT_MODEL)}",
                    style="green",
                )
            )

        # The router is middleware, so the running agent has to be rebuilt for the
        # change to apply. Rebuild on the live model rather than a fresh
        # create_model(): the user's /model choice is not what changed here.
        live_model = getattr(self.session_state, "_model", None)
        if live_model is None:
            self._log(Text("Saved; applies from your next session.", style="yellow"))
            return
        try:
            self.agent, self.backend = await self.session_state.switch_model(live_model)
        except Exception:  # noqa: BLE001 — settings persist even if rebuilding fails
            self._log(Text("Saved; restart Nova to apply them.", style="yellow"))

    def _apply_role_pick(self, result: dict, role: str) -> None:
        """Save a model for one non-main role, and say when it takes effect.

        The main agent keeps ``/model``'s original behaviour and hot-swaps the live
        agent. Every other role is a setting that applies when it is next used, so
        this writes it, drops the cached subagent specs (they are cached for 60s,
        which would otherwise make the change look ignored), and reports both facts
        rather than implying the running agent changed.
        """
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli.config.role_models import ROLE_LABELS

        provider = str(result.get("provider") or "")
        model = str(result.get("model") or "")
        if not provider or not model:
            self._log(Text("That row had no usable model to save.", style="red"))
            return
        try:
            NovaConfig().set_role_model(role, provider, model, self._credential_base_url(provider))
            from novacode_cli.agents.core_agent import clear_named_subagents_cache

            clear_named_subagents_cache()
        except Exception as ex:  # noqa: BLE001 — a pick must never crash the TUI
            label = ROLE_LABELS.get(role, role)
            self._log(Text(f"Could not set the {label} model: {ex}", style="red"))
            return
        when = (
            "on the next server launch (recreate the container to pick it up)"
            if role == "async"
            else "on the next dispatch"
        )
        self._log(
            Text(
                f"● {ROLE_LABELS.get(role, role)} → {provider}:{model} — takes effect {when}",
                style="green",
            )
        )

    def _apply_voice_pick(self, result: dict) -> None:
        """Persist a voice row chosen in the picker and rebuild the pipeline.

        The cached pipeline has to go: `_ensure_voice_pipeline` returns early
        while one exists, so a config change alone would look like the picker did
        nothing. This mirrors what ``/voice settings`` does in ``_run_voice``.

        Args:
            result: The picker's payload, carrying ``space``, ``provider``,
                ``field`` and ``model`` (the voice/model name).
        """
        from novacode_cli.audio.providers import STT_PROVIDERS, TTS_PROVIDERS
        from novacode_cli.config.nova_config import NovaConfig

        space = str(result.get("space") or "")
        provider = str(result.get("provider") or "")
        field = str(result.get("field") or "model")
        name = str(result.get("model") or "")
        registry = STT_PROVIDERS if space == "stt" else TTS_PROVIDERS
        meta = registry.get(provider) or {}
        if not name:
            return

        try:
            config = NovaConfig()
            config.set_voice_provider_config(provider, **{field: name})
            # Selecting a voice also makes its provider the active one for that
            # axis: choosing "nova-3" while faster-whisper is active would
            # otherwise save a setting with no effect.
            config.set_voice_config(**{f"{space}_provider": provider})
        except Exception as ex:  # noqa: BLE001 — a config failure must be visible
            self._log(Text(f"Could not save the voice setting: {ex}", style="red"))
            return

        self._voice_pipeline = None
        if hasattr(self.session_state, "_voice_pipeline"):
            self.session_state._voice_pipeline = None

        label = meta.get("name") or provider
        axis = "speech to text" if space == "stt" else "text to speech"
        self._log(Text(f"● {label} · {name} for {axis}", style="green"))
        if meta.get("requires_key"):
            self._log(Text("Used on the next capture; /voice test to check it.", style="dim"))

    @staticmethod
    def _credential_base_url(provider: str) -> str:
        """Return the endpoint paired with *provider*'s stored credential.

        Empty for a provider that takes no endpoint, and empty when the stored
        credential has none — both mean "use the provider default".

        Args:
            provider: Provider id.

        Returns:
            The endpoint URL, or an empty string.
        """
        from novacode_cli.config.credentials import credential_meta
        from novacode_cli.config.model_manager import ENDPOINT_PROVIDERS
        from novacode_cli.config.provider_auth import credential_env_var

        if provider not in ENDPOINT_PROVIDERS:
            return ""
        env_var = credential_env_var(provider)
        meta = credential_meta(env_var) if env_var else None
        return (meta.base_url or "") if meta is not None else ""

    @staticmethod
    def _remember_model(provider: str, model: str) -> None:
        """Record a successful pick so the picker can pin it next time.

        Args:
            provider: Provider id that was switched to.
            model: Model id that was switched to.
        """
        try:
            from novacode_cli.config.nova_config import NovaConfig

            NovaConfig().push_recent_model(f"{provider}:{model}")
        except Exception:  # noqa: BLE001 — recents are a convenience, never a failure
            pass

    async def _run_auth(self, provider: str | None = None) -> None:
        """Native /auth: manage the API keys stored on this machine.

        Keys live in the OS credential store, so what is added here applies to
        every Nova session rather than to this one. `/model` routes here when it
        is handed a provider with no usable credential.

        Args:
            provider: Optional credential name (provider or service id) whose row
                should start highlighted.
        """
        from novacode_cli.config.model_manager import MODEL_PRESETS, ModelManager
        from novacode_cli.config.provider_auth import provider_display_name
        from novacode_cli.tui.auth_screens import AuthManagerScreen

        screen = AuthManagerScreen(focus=provider)
        await self.push_screen_wait(screen)

        if not (screen.saved or screen.deleted):
            return

        for name in screen.saved:
            self._log(Text(f"● Saved the API key for {provider_display_name(name)}", style="green"))
            if name == "tavily":
                # tools/web_tools.py builds its Tavily client at import time, so
                # a key added now only gates web_search after a restart.
                self._log(Text("Web search picks it up after a restart.", style="yellow"))
            if name == "jev":
                self._log(Text("Configure Jev in /model → Decisions.", style="dim"))
        for name in screen.deleted:
            self._log(
                Text(
                    f"Deleted the stored API key for {provider_display_name(name)}",
                    style="yellow",
                )
            )

        # Say what the change was worth: the point of storing a key is the
        # provider becoming usable, and that count is the honest summary.
        available = len(ModelManager().get_available_providers())
        self._log(
            Text(
                f"{available} of {len(MODEL_PRESETS)} providers configured (/model to switch).",
                style="dim",
            )
        )

    async def _run_session_import(self, text: str) -> None:
        from novacode_cli.tui.session_import import dispatch_import_command

        await dispatch_import_command(self, text)

    async def _run_sessions(self) -> None:
        """Open the saved-sessions screen (list + delete)."""
        from novacode_cli.session.session_persistence import SessionManager

        sm = self.session_manager or SessionManager()
        await self.push_screen_wait(
            SessionsScreen(sm, getattr(self.session_state, "session_id", None))
        )

    async def _run_resume(self, text: str) -> None:
        """Resume a saved session for the current path.

        ``/resume``       → pick from sessions saved for THIS workspace.
        ``/resume <id>``  → resume that session id directly (any path).

        The current conversation is auto-saved first, then replaced by the
        chosen session's history and identity on a clean, fresh thread — the
        same continuation build the ``nova --continue`` startup path uses.
        """
        if self.session_manager is None:
            self._log(Text("Session resume is unavailable.", style="yellow"))
            return

        from novacode_cli.config.config import (
            get_default_coding_instructions,
            settings,
        )
        from novacode_cli.session.session_prompt_builder import (
            build_continuation_prompt,
            load_NOVA_md,
        )
        from novacode_cli.session.session_restore import (
            _truncate,
            format_session_age,
            restore_session,
        )
        from novacode_cli.tracking.workspace_anchoring import scan_workspace

        sm = self.session_manager
        workspace_root = settings.get_workspace_root()
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Resolve the target session id: explicit arg, else a cwd-filtered picker.
        if arg:
            target_id: str | None = arg
        else:
            ws = str(workspace_root)
            current_id = getattr(self.session_state, "session_id", None)
            sessions = [
                s
                # Reads every session's metadata file: off the UI loop.
                for s in await asyncio.to_thread(sm.list_sessions, limit=200)
                if s.project_root == ws and s.session_id != current_id
            ][:20]
            if not sessions:
                self._log(
                    Text(
                        "No saved sessions for this path yet. "
                        "(Sessions autosave as you work; use /save to force one.)",
                        style="yellow",
                    )
                )
                return
            options = [
                f"{s.session_id[:8]}  ({s.model_name or 'unknown'}) · "
                f"{s.message_count} msgs · {format_session_age(s.last_active)} · "
                f"{_truncate(getattr(s, 'current_task', None), 40) or '—'}"
                for s in sessions
            ]
            idx = await self.push_screen_wait(
                PickScreen(
                    "Resume a session (this path)",
                    options,
                    hint="↑/↓ select · Enter resume · Esc cancel",
                )
            )
            if not (0 <= idx < len(sessions)):
                return
            target_id = sessions[idx].session_id

        # Load the session and build the continuation prompt off the event loop
        # (mirrors the --continue startup path in main.py).
        result = await asyncio.to_thread(restore_session, sm, target_id, workspace_root)
        if not result:
            self._log(Text(f"Session '{target_id[:8]}' not found.", style="yellow"))
            return
        session_data, warnings = result

        recent_messages, current_workspace, nova_md = await asyncio.gather(
            asyncio.to_thread(sm.load_recent_messages, session_data.meta.session_id),
            asyncio.to_thread(scan_workspace, workspace_root),
            asyncio.to_thread(load_NOVA_md, workspace_root),
        )
        session_data.messages = list(session_data.messages or []) or recent_messages
        initial_messages = build_continuation_prompt(
            session_data=session_data,
            system_prompt=get_default_coding_instructions(),
            NOVA_md_content=nova_md,
            workspace_state=current_workspace,
        )

        # Auto-save the CURRENT conversation before replacing it, then take a
        # clean slate (fresh thread_id → empty checkpointer) and adopt the
        # resumed session's identity + todos.
        await self._save_session()
        resumed_id = session_data.meta.session_id
        resumed_todos = session_data.todos
        self.session_state.reset_conversation()  # fresh thread_id, cleared state
        self.session_state.session_id = resumed_id
        self._refresh_session_identity()
        self.session_state.is_continued = True
        if resumed_todos:
            self.session_state.todos = resumed_todos

        # Re-own any live sandbox to the resumed session id so the orphan sweep
        # never reclaims a container this chat still uses (mirrors /clear).
        if self._sandbox_id:
            try:
                from novacode_cli.integrations import sandbox_registry

                sandbox_registry.retie(self._sandbox_id, resumed_id)
            except Exception:  # noqa: BLE001
                pass

        # Restore the resumed session's own model, the way the `--continue`
        # startup path does. Without this the agent kept running on whatever
        # model the *previous* conversation was using, so the picker at the top
        # of this command showed the session's recorded model while the chat
        # quietly ran on a different one -- and the next autosave overwrote the
        # recorded model with the live one, losing the session's original
        # model for good.
        #
        # Non-fatal by design: a model that cannot be rebuilt must never abort
        # the resume. Losing the conversation over a missing API key would be
        # strictly worse than continuing on the current model, so every failure
        # below falls through and says why.
        resumed_provider = getattr(session_data.meta, "model_provider", None)
        if resumed_provider:
            try:
                from novacode_cli.config.model_create import create_model_for_session

                resumed_model, model_warning = create_model_for_session(
                    resumed_provider,
                    getattr(session_data.meta, "model_name", None),
                )
                if resumed_model is not None:
                    # switch_model() takes only the model: it reads session_id
                    # off session_state, which is already resumed_id from the
                    # reset above, so hook dispatch attributes the rebuilt
                    # agent to the resumed session.
                    new_agent, new_backend = await self.session_state.switch_model(
                        resumed_model,
                    )
                    self.agent = new_agent
                    self.backend = new_backend
                    self.model_name = (
                        getattr(resumed_model, "model_name", None)
                        or getattr(resumed_model, "model", None)
                        or self.model_name
                    )
                    # Keep the recorded provider in step with the live model, so
                    # the next save records the model we actually switched to
                    # rather than the one we just replaced.
                    self._model_provider = resumed_provider
                    if self.token_tracker is not None:
                        try:
                            self.token_tracker.set_model(self.model_name)
                        except Exception:  # noqa: BLE001
                            pass
                    self._log(
                        Text(
                            f"● Restored session model {resumed_provider}:{self.model_name}.",
                            style="green",
                        )
                    )
                elif model_warning:
                    # (None, warning): the session recorded a model that cannot
                    # be rebuilt now. Warn and keep the current one.
                    warnings.append(model_warning)
            except Exception as exc:  # noqa: BLE001
                self._log(Text(f"Model restore failed: {exc}", style="red"))
        # (None, None) is a legacy session that predates provider recording:
        # nothing to restore, so no warning and no wasted agent rebuild.

        # Seed the fresh thread with the continuation history.
        from novacode_cli.session.imported_context import (
            MESSAGE_ID,
            estimated_tokens,
            import_budget,
            restore_imported_reference,
        )

        sessions_dir = getattr(sm, "sessions_dir", None)
        if sessions_dir is not None:
            retained = [message for message in initial_messages if message.id != MESSAGE_ID]
            window = getattr(self.token_tracker, "context_window_size", 128000)
            used = estimated_tokens("\n".join(str(message.content) for message in retained))
            initial_messages = await asyncio.to_thread(
                restore_imported_reference,
                initial_messages,
                sessions_dir,
                resumed_id,
                import_budget(window, used),
            )
        config = {"configurable": {"thread_id": self.session_state.thread_id}}
        try:
            await self.agent.aupdate_state(config, values={"messages": initial_messages})
        except Exception as exc:  # noqa: BLE001
            self._log(Text(f"Resume failed while seeding state: {exc}", style="red"))
            return

        # Reset per-conversation UI/tracking and re-baseline token accounting.
        self._reset_streaming()
        self._clear_live_steers()
        self._seen.clear()
        if self.token_tracker is not None:
            try:
                self.token_tracker.reset()
            except Exception:  # noqa: BLE001
                pass

        # Replace the transcript with the resumed session's recent history.
        await self._transcript().remove_children()
        self._restored_messages = list(session_data.messages or recent_messages or [])
        self._replay_history()
        # Paint the resumed checklist. Previously the restored todos only
        # reached session_state and nothing rendered them, so a resumed
        # session showed no todos until the agent next emitted an update.
        self._todos = list(resumed_todos or [])
        self._todos_agent = None
        self._paint_todos(self._todos)
        self._update_mode_badge()
        # Re-measure ctx from the seeded history. `token_tracker.reset()` above
        # drops back to the baseline, which is correct for the API counters (no
        # call has been made on this thread yet) but wrong for the display: the
        # continuation prompt is already in the window, so ctx% read ~0 until
        # the next turn finished. Measuring the state here shows the real size.
        await self._update_context_breakdown()
        self._refresh_status()

        for w in warnings:
            self._log(Text(f"⚠ {w}", style="yellow dim"))
        self._log(
            Text(
                f"● Resumed session {resumed_id[:8]} — "
                f"{len(recent_messages or [])} recent message(s) replayed.",
                style="green",
            )
        )

    async def _run_artifacts(self) -> None:
        """Open the artifacts list (same as clicking the ◈ Artifacts component)."""
        self._open_artifacts_list()

    async def _run_subagents(self) -> None:
        """Open the subagents manager, where both kinds are created.

        One file makes either kind: an agent is in-process by default, and
        `async: true` in its frontmatter also runs it in the background on the
        agent server. `alt+s`, or clicking the dock, still toggles the live
        panel.
        """
        await self.push_screen_wait(AgentsScreen())

    async def _run_agent_server(self, text: str = "") -> None:
        """The local LangGraph server the async subagents run on.

        Status is the default; the actions exist because a launch that fails is
        otherwise invisible (it soft-fails to the in-process subagents), so there
        has to be somewhere to look.
        """
        from novacode_cli.agents import server_launcher
        from novacode_cli.agents.default_subagents.async_subagents import (
            async_agents_available,
        )

        parts = (text or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        status = server_launcher.agent_server_status()

        if action in ("", "status"):
            reachable = await asyncio.to_thread(async_agents_available, refresh=True)
            from novacode_cli.agents.default_subagents.async_subagents import (
                async_agents_see_workspace,
            )

            here = await asyncio.to_thread(async_agents_see_workspace)
            lines = [
                f"local server : {'running' if status['running'] else 'stopped'}"
                + (f" at {status['url']}" if status["url"] else ""),
                f"async agents : {'available' if reachable else 'unavailable'}"
                + (" (this session's server)" if status["running"] else "")
                # Reachable is not the same as usable: the server reads its own
                # root, so in another project it is deliberately left unused.
                + (
                    " — not used here: the server is rooted at another project"
                    if reachable and not here
                    else ""
                ),
                "extra        : "
                + (
                    "installed"
                    if server_launcher.server_extra_available()
                    else "missing (uv sync --extra agents-server)"
                ),
            ]
            if status["log_path"]:
                lines.append(f"log          : {status['log_path']}")
            if status["graphs"]:
                lines.append(f"graphs       : {len(status['graphs'])} registered")
            self._log(Text("agent server", style=f"bold {self._palette.primary}"))
            for line in lines:
                self._log(Text(line, style="dim"))
            return

        if action == "logs":
            if not status["log_path"]:
                self._log(Text("no agent server log: nothing has been launched", style="dim"))
                return
            tail = await asyncio.to_thread(_read_tail, str(status["log_path"]))
            self._log(Text("agent server log (tail)", style=f"bold {self._palette.primary}"))
            for line in tail.splitlines()[-20:]:
                self._log(Text(line, style="dim"))
            return

        if action == "stop":
            preserved = await asyncio.to_thread(server_launcher.shutdown_agent_server)
            note = f" (log kept at {preserved})" if preserved else ""
            self._log(Text(f"agent server: stopped{note}", style="dim"))
            return

        if action in ("start", "restart"):
            if action == "restart":
                await asyncio.to_thread(server_launcher.shutdown_agent_server)
            started = await server_launcher.ensure_agent_server()
            if started is None:
                self._log(
                    Text(
                        "agent server: not started - another server already answers, "
                        "or the agents-server extra is not installed",
                        style=f"bold {self._palette.warning}",
                    )
                )
                return
            self._log(Text(f"agent server: running at {started.url}", style="dim"))
            return

        self._log(
            Text(
                "usage: /agent-server [status|start|stop|restart|logs]",
                style=f"bold {self._palette.warning}",
            )
        )

    async def _run_tasks(self) -> None:
        """Open the Background Tasks panel (same as clicking the ● indicator)."""
        self._open_tasks_panel()

    async def _run_cowork(self, text: str) -> None:
        """Launch (or focus) the Nova Cowork desktop app in the browser.

        Reuses Nova's FastAPI agent server + the /cowork UI + WorkspacePolicy
        broker. Server startup is heavy, so it runs off the event loop.
        """
        parts = text.split(maxsplit=1)
        task = parts[1].strip() if len(parts) > 1 else None
        self._log(
            Text("◆ Launching Nova Cowork desktop… (grant a folder to begin)", style="#7aa2f7")
        )
        self._launch_cowork(task)

    @work(thread=True, exclusive=True, group="cowork")
    def _launch_cowork(self, task: str | None) -> None:
        try:
            from novacode_cli.cowork.launcher import cowork_url

            sid = getattr(self.session_state, "session_id", None)
            url = cowork_url(session_id=sid, task=task)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log, Text(f"Cowork failed to launch: {e}", style="red"))
            return
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        self.call_from_thread(
            self._log,
            Text.assemble(("◆ Nova Cowork ready — ", "bold #7aa2f7"), (url, "underline #7aa2f7")),
        )

    async def _run_mcp(self) -> None:
        """Open the MCP servers screen (view + remove)."""
        await self.push_screen_wait(McpScreen())

    def _build_init_agent(self) -> tuple[Any, Any]:
        """Build a dedicated **no-HITL, LOCAL-filesystem** agent for /init.

        Two deliberate deviations from the session agent:

        1. ``auto_approve=True`` → ``interrupt_on={}``: the main agent AND every
           subagent (incl. deepagents' auto-injected general-purpose one) run
           tools without approval interrupts. A subagent's HITL interrupt is
           unresolvable (it bubbles out of `task`'s ainvoke as a GraphInterrupt),
           so this is required for /init's `task` workers to run unattended.

        2. ``sandbox=None`` → a LOCAL FilesystemBackend rooted at the project
           (virtual_mode). /init is inherently a local operation: graphify reads
           the local files, and the graph fragments must be written to the local
           ``.nova/graph_fragments/`` so `_read_and_merge_fragments` can read
           them back. Running through the session's *sandbox* backend broke this
           two ways — `/`-prefixed virtual paths resolve to the container root
           (project is at ``/workspace`` → every read_file 404'd), and any
           fragment write would land inside the sandbox, invisible to the local
           merge step. Forcing the local backend fixes both.

        Reuses the session model/tools/store. Raises if no model is configured.
        """
        from novacode_cli.agents.core_agent import create_agent_with_config

        ss = self.session_state
        model = getattr(ss, "_model", None)
        if model is None:
            raise RuntimeError("no model configured")
        return create_agent_with_config(
            model=model,
            assistant_id=getattr(ss, "_assistant_id", None) or self.assistant_id,
            tools=getattr(ss, "_tools", None) or [],
            sandbox=None,  # ← LOCAL filesystem (see docstring #2)
            sandbox_type=None,
            store=getattr(ss, "_store", None),
            checkpointer=getattr(ss, "_checkpointer", None),
            auto_approve=True,  # ← no HITL anywhere (see docstring #1)
            session_id=getattr(ss, "session_id", None) or getattr(ss, "thread_id", None),
        )

    async def _run_init(self, text: str) -> None:
        """Generate NOVA.md: delegates orchestration to :class:`InitOrchestrator`."""
        from pathlib import Path

        from novacode_cli.commands.init_handler import InitFlags, InitOrchestrator
        from novacode_cli.config.config import settings

        cmd_args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else None
        project_root = settings.project_root
        if not project_root:
            self._log(Text("/init requires a project with a .git directory.", style="yellow"))
            return
        nova_md_path = Path(project_root) / ".nova" / "NOVA.md"
        self._log(Text(f"🔍 Initializing NOVA.md for {Path(project_root).name}…", style="bold"))

        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("exploring codebase…")
        _prev_auto = self.session_state.auto_approve
        self.session_state.auto_approve = True

        try:
            renderer = TuiInitRenderer(self)
            orchestrator = InitOrchestrator(
                project_root=Path(project_root),
                nova_md_path=nova_md_path,
                flags=InitFlags(cmd_args),
                renderer=renderer,
                agent=self.agent,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
                token_tracker=self.token_tracker,
            )
            await orchestrator.run()
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/init failed: {ex}", style="red"))
        finally:
            self.session_state.auto_approve = _prev_auto
            self._turn_active = False
            self._set_status("ready")

        if nova_md_path.exists():
            self._log(Text(f"● NOVA.md ready → {nova_md_path}", style="green"))

    async def _run_trace(self, text: str) -> None:
        """Native LangSmith tracing: status / enable / disable / projects / traces."""
        import os

        parts = text.split()
        args = parts[1:]
        sub = parts[1].lower() if len(parts) > 1 else "status"
        from novacode_cli.tracking.tracing import (
            configure_tracing,
            get_traces,
            get_tracing_config,
            get_tracing_status,
            list_projects,
        )

        if sub in ("enable", "on"):
            api_key = args[1] if len(args) > 1 and not args[1].startswith("-") else None
            project_name = None
            for i, a in enumerate(args):
                if a == "--project" and i + 1 < len(args):
                    project_name = args[i + 1]
                    break
            cfg = configure_tracing(api_key=api_key, project_name=project_name, enable=True)
            if cfg.is_configured():
                t = Text()
                t.append("● LangSmith tracing enabled\n", style="green")
                t.append(f"  project: {cfg.project_name}\n", style="dim")
                t.append("  set LANGSMITH_API_KEY in .env to persist\n", style="dim")
                self._log(t)
            else:
                self._log(
                    Text(
                        "Failed to enable tracing — LANGSMITH_API_KEY is required.",
                        style="red",
                    )
                )
            return
        if sub in ("disable", "off"):
            os.environ["LANGSMITH_TRACING"] = "false"
            self._log(
                Text(
                    "● LangSmith tracing disabled for this session "
                    "(set LANGSMITH_TRACING=false in .env to persist).",
                    style="green",
                )
            )
            return
        if sub == "projects":
            projects = list_projects()
            t = Text()
            t.append("LangSmith projects\n", style="bold")
            if projects:
                for p in projects[:20]:
                    t.append(f"  {p['name']}", style="cyan")
                    t.append(f"  {p['url']}\n", style="dim")
            else:
                t.append("  (none found or tracing not configured)\n", style="dim")
            self._log(t)
            return
        if sub in ("traces", "recent"):
            limit = 10
            for i, a in enumerate(args):
                if a in ("-n", "--limit") and i + 1 < len(args):
                    try:
                        limit = int(args[i + 1])
                    except ValueError:
                        pass
            traces = get_traces(limit=limit)
            t = Text()
            t.append(f"Recent traces (last {limit})\n", style="bold")
            if traces:
                for tr in traces[:limit]:
                    created = (tr.get("created_at", "") or "")[:19] or "unknown"
                    inputs = str(tr.get("inputs", {}))[:40]
                    t.append(f"  {tr['name']}", style="cyan")
                    t.append(f"  {created}  {inputs}\n", style="dim")
            else:
                t.append(
                    "  (none — make a request with tracing enabled first)\n",
                    style="dim",
                )
            self._log(t)
            return
        if sub in ("-h", "--help", "help"):
            t = Text()
            t.append("/trace — LangSmith tracing\n", style="bold")
            for name, desc in (
                ("status", "show current tracing configuration"),
                ("enable [KEY] [--project P]", "enable tracing"),
                ("disable", "disable tracing for this session"),
                ("projects", "list LangSmith projects"),
                ("traces [--limit N]", "show recent traces"),
            ):
                t.append(f"  /trace {name}\n", style="cyan")
                t.append(f"      {desc}\n", style="dim")
            self._log(t)
            return
        if sub not in ("status", ""):
            self._log(Text(f"Unknown trace subcommand: {sub} (try /trace help)", style="yellow"))
            return

        st = get_tracing_status()
        t = Text()
        t.append("LangSmith tracing\n", style="bold")
        if not st.get("available"):
            t.append("  langsmith not installed\n", style="dim")
        elif st.get("configured"):
            cfg = get_tracing_config()
            t.append("  ● enabled\n", style="green")
            t.append(f"  project: {cfg.project_name}\n", style="dim")
            if getattr(cfg, "workspace_id", None):
                t.append(f"  workspace: {cfg.workspace_id}\n", style="dim")
            t.append("  view: https://smith.langchain.com\n", style="dim")
        else:
            t.append("  ○ not configured\n", style="yellow")
            t.append("  set LANGSMITH_API_KEY and LANGSMITH_TRACING=true\n", style="dim")
        self._log(t)

    async def _run_log(self, text: str) -> None:
        """Native recent-runs list and `/log show <id>` detail."""
        from novacode_cli.commands.log_commands import (
            _list_runs,
            _load_json,
            _runs_dir,
        )

        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "list"
        ws = str(getattr(self.session_state, "workspace_root", "") or "") or None
        runs_dir = _runs_dir(ws)

        if sub == "show" and len(parts) > 2:
            run_id = parts[2]
            matches = [p for p in _list_runs(runs_dir) if p.name.startswith(run_id)]
            if not matches:
                self._log(Text(f"No run matching '{run_id}'", style="red"))
                return
            run_dir = matches[0]
            t = Text()
            t.append(f"Run: {run_dir.name}\n", style="bold")
            for label, fname in (
                ("Meta", "meta.json"),
                ("Summary", "summary.json"),
                ("Verdict", "user_verdict.json"),
            ):
                data = _load_json(run_dir / fname)
                if data:
                    t.append(f"\n{label}\n", style="bold")
                    for k, v in data.items():
                        t.append(f"  {k}: {v}\n", style="dim")
            self._log(t)
            return

        if sub == "grep":
            import re as _re

            if len(parts) < 3:
                self._log(Text("Usage: /log grep <pattern>", style="red"))
                return
            pattern = parts[2]
            limit = 50
            for i, a in enumerate(parts):
                if a == "--limit" and i + 1 < len(parts):
                    try:
                        limit = int(parts[i + 1])
                    except ValueError:
                        pass
            try:
                rx = _re.compile(pattern, _re.IGNORECASE)
            except _re.error as e:
                self._log(Text(f"Invalid pattern: {e}", style="red"))
                return

            def _scan() -> tuple[Text, int]:
                """Walk the run history and build the report, off the loop.

                Every call in here is synchronous filesystem work (`_list_runs`,
                `iterdir`, `exists`, `read_text`), so running it on the loop
                blocks the whole TUI for the length of the walk. It returns the
                report and the hit count instead of logging, because `self._log`
                touches widgets and must not run on a worker thread.
                """
                out = Text()
                out.append(f"grep '{pattern}'\n", style="bold")
                found = 0
                for run_dir in _list_runs(runs_dir):
                    turns_dir = run_dir / "turns"
                    if not turns_dir.exists():
                        continue
                    for turn in sorted(turns_dir.iterdir()):
                        for fname in ("prompt.txt", "response.json"):
                            fpath = turn / fname
                            if not fpath.exists():
                                continue
                            content = fpath.read_text(encoding="utf-8", errors="replace")
                            for lineno, line in enumerate(content.splitlines(), 1):
                                if rx.search(line):
                                    out.append(
                                        f"  {run_dir.name[:16]}/{turn.name}/{fname}:{lineno}  ",
                                        style="dim",
                                    )
                                    out.append(f"{line.strip()[:120]}\n")
                                    found += 1
                                    if found >= limit:
                                        out.append(f"  … stopped at {limit} hits\n", style="dim")
                                        return out, found
                return out, found

            t, hits = await asyncio.to_thread(_scan)
            if hits == 0:
                t.append(f"  (no matches for '{pattern}')\n", style="dim")
            self._log(t)
            return

        if sub not in ("list", ""):  # diff / verdict / frontier
            await self._passthrough_command(text)
            return

        # /log list → recent interactive SESSIONS. The .nova/runs/ turn format the
        # other subcommands read is only produced by the offline eval harness;
        # interactive work is recorded under ~/.nova/sessions instead, so list
        # from there (that's what "recent runs" means in normal use).
        from novacode_cli.session.session_persistence import SessionManager

        sm = self.session_manager or SessionManager()
        try:
            sessions = await asyncio.to_thread(sm.list_sessions, limit=20)
        except Exception:  # noqa: BLE001
            sessions = []
        t = Text()
        t.append("Recent sessions\n", style="bold")
        if not sessions:
            t.append("  (no sessions yet — start chatting to record one)\n", style="dim")
        else:
            for s in sessions:
                t.append(f"  {self._session_log_line(s)}\n", style="dim")
        self._log(t)

    @staticmethod
    def _session_log_line(s: Any) -> str:
        """One-line summary of a saved session for /log list."""
        sid = (getattr(s, "session_id", "") or "")[:8]
        model = getattr(s, "model_name", None) or "?"
        msgs = getattr(s, "message_count", 0)
        status = getattr(s, "task_status", "") or ""
        when = getattr(s, "last_active", "") or ""
        try:
            from datetime import datetime

            when = datetime.fromisoformat(when).strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            when = when[:16]
        task = getattr(s, "current_task", None)
        task_str = f"  · {task[:40]}" if task else ""
        return f"{sid}  {when}  msgs={msgs}  {status}  {model}{task_str}"

    # -- plan mode ------------------------------------------------------------
    def _active_agent(self) -> tuple[Any, Any]:
        """Route to the plan agent while plan mode is active, else the main agent.

        During /init a dedicated no-HITL agent (``_init_agent``) takes priority so
        the pipeline's `task` subagents can read/write files unattended — the
        shared session agent gates those tools and a subagent's interrupt is
        unresolvable (it bubbles out of `task`'s ainvoke as a GraphInterrupt).
        """
        init_agent = getattr(self, "_init_agent", None)
        if init_agent is not None:
            return init_agent, getattr(self, "_init_backend", None)
        if getattr(self.session_state, "plan_mode_enabled", False) and (
            getattr(self.session_state, "plan_agent", None) is not None
        ):
            return self.session_state.plan_agent, getattr(self.session_state, "plan_backend", None)
        return self.agent, self.backend

    async def _enable_plan_mode(self) -> bool:
        try:
            from novacode_cli.agents.plan_agent import create_plan_agent_with_config
            from novacode_cli.tools.plan_mode_tools import (
                ask_user_question,
                enter_plan_mode,
                exit_plan_mode,
            )

            model = getattr(self.session_state, "_model", None)
            if model is None:
                self._log(Text("Plan mode needs a model; none configured.", style="red"))
                return False
            plan_agent, plan_backend = create_plan_agent_with_config(
                model=model,
                assistant_id=getattr(self.session_state, "_assistant_id", None) or "nova",
                tools=[ask_user_question, enter_plan_mode, exit_plan_mode],
                steering_instructions=getattr(self.session_state, "steering_instructions", None),
                auto_approve=getattr(self.session_state, "auto_approve", False),
                # Share the core agent's checkpointer + store so plan mode sees
                # the ongoing conversation (same thread_id) and persists.
                checkpointer=getattr(self.session_state, "_checkpointer", None),
                store=getattr(self.session_state, "_store", None),
            )
            self.session_state.plan_mode_enabled = True
            self._update_mode_badge()
            self.session_state.plan_content = None
            self.session_state.approved_plan_content = None
            if not getattr(self.session_state, "auto_approve", False):
                self.session_state.auto_approve = False
            self.session_state.plan_agent = plan_agent
            self.session_state.plan_backend = plan_backend
            return True
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Plan mode failed: {ex}", style="red"))
            self.session_state.plan_mode_enabled = False
            self._update_mode_badge()
            return False

    async def _maybe_run_approved_plan(self) -> None:
        """After a plan-mode turn, execute an approved plan via the main agent.

        Clears the agent's conversation context first (fresh thread_id, empty
        checkpointer state) so execution starts with a clean slate — just the
        plan, no history from the planning conversation. The TUI transcript is
        also cleared so the user sees a fresh execution view.
        """
        try:
            approved = self.session_state.consume_approved_plan()
        except Exception:  # noqa: BLE001
            approved = None
        try:
            if approved:
                try:
                    self.session_state.clear_plan_agent()
                except Exception:  # noqa: BLE001
                    pass

                # Clear conversation context so the agent starts fresh with
                # only the plan execution instruction — no planning history.
                self.session_state.reset_conversation()
                await self._transcript().remove_children()
                self._show_home_banner()
                self._log(Text("● Plan approved — starting fresh execution…", style="cyan"))
                _tid = getattr(self.session_state, "thread_id", "?")
                self._log(
                    Text(
                        f"[plan-debug] executing on thread {str(_tid)[:8]}, plan {len(approved)} chars",
                        style="dim",
                    )
                )
                await self._stream_prompt(
                    "The user has approved the following plan. Execute it step by step, "
                    "marking each step complete as you go:\n\n" + approved
                )
                try:
                    _st = await self.agent.aget_state({"configurable": {"thread_id": _tid}})
                    _msgs = _st.values.get("messages", []) if _st else []
                    _kinds = [type(m).__name__ for m in _msgs][-6:]
                    self._log(
                        Text(
                            f"[plan-debug] after execution: {len(_msgs)} msgs, last={_kinds}",
                            style="dim",
                        )
                    )
                except Exception as _ex:  # noqa: BLE001
                    self._log(Text(f"[plan-debug] state read failed: {_ex}", style="dim red"))
        finally:
            # "Auto-approve edits" was scoped to this plan run — restore, so
            # the NEXT plan prompts for approval again instead of silently
            # self-approving (see the plan-approval modal handler).
            if getattr(self, "_plan_scoped_auto_approve", False):
                self._plan_scoped_auto_approve = False
                self.session_state.auto_approve = False

    async def _run_plan(self, text: str) -> None:
        """Native /plan: status / off / enable (+ optional prompt)."""
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        low = args.lower()
        if low == "status":
            enabled = getattr(self.session_state, "plan_mode_enabled", False)
            self._log(
                Text(
                    f"Plan mode: {'enabled' if enabled else 'disabled'}",
                    style="cyan" if enabled else "dim",
                )
            )
            return
        if low == "off":
            self.session_state.plan_mode_enabled = False
            self._update_mode_badge()
            try:
                self.session_state.clear_plan_agent()
            except Exception:  # noqa: BLE001
                pass
            self._log(Text("Plan mode disabled.", style="yellow"))
            return
        if not await self._enable_plan_mode():
            return
        self._log(
            Text(
                "▌ Plan mode is Active",
                style="cyan",
            )
        )
        if args:
            await self._stream_prompt(args)
            await self._maybe_run_approved_plan()

    async def _run_goal(self, text: str) -> None:
        """Set, show, or clear the active goal for autonomous goal-mode execution.

        Usage:
          /goal <description>   — set the goal and kick off the agent
          /goal status          — show the current goal
          /goal clear           — remove the active goal
        """
        from novacode_cli.commands.side_commands import handle_goal_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        # Shared parse + state mutation (kept identical across TUI / REPL / remote).
        result = handle_goal_command(self.session_state, args)

        if result.action == "status":
            if result.goal:
                t = Text()
                t.append("🎯 Active goal\n", style="bold #e0af68")
                t.append(result.goal, style="italic")
                self._log(t)
            else:
                self._log(Text(result.message, style="dim"))
            return

        if result.action == "clear":
            self._update_mode_badge()
            self._log(Text("Goal cleared.", style="yellow"))
            return

        if result.action == "usage":
            self._log(Text(result.message, style="dim"))
            return

        # action == "set"
        self._update_mode_badge()
        t = Text()
        t.append("🎯 Goal set\n", style="bold #e0af68")
        t.append(result.goal or "", style="italic")
        self._log(t)

        if result.kickoff:
            await self._stream_prompt(result.kickoff)

    # -- btw (concurrent side-channel question with web search) ----------------

    def _get_btw_agent(self) -> Any:
        """Return the cached btw agent (shared process-wide with the remote bridge)."""
        from novacode_cli.commands.side_commands import get_btw_agent

        return get_btw_agent()

    async def _run_btw(self, text: str) -> None:
        """Dispatch a /btw side question — runs concurrently with the main agent."""
        import uuid

        parts = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""

        if not question:
            self._log(Text("Usage: /btw <question>", style="dim"))
            return

        try:
            agent = self._get_btw_agent()
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"↩ btw: could not start web-search agent — {ex}", style="red"))
            return

        thread_id = f"btw-{uuid.uuid4().hex[:12]}"
        self._btw_worker(question, agent, thread_id)

    @work(group="btw")
    async def _btw_worker(self, question: str, agent: Any, thread_id: str) -> None:
        """Run the btw question on its own thread, concurrently with the main agent.

        Uses a dedicated ``group="btw"`` work group so it never blocks — or is
        blocked by — the main ``group="turn"`` agent. Multiple /btw calls queue
        within the "btw" group and run one at a time (sequential but not blocking
        the main UI).
        """
        from novacode_cli.agent_stream import run_agent_stream
        from novacode_cli.ui_events import AssistantMessage, Done, Error

        # Minimal session-state shim: only thread_id matters for the btw agent
        # (no checkpointer sharing, no goal/plan injection).
        class _BtwSession:
            def __init__(self) -> None:
                self.thread_id = thread_id
                self.active_goal: str | None = None
                self.plan_mode_enabled: bool = False
                self.auto_approve: bool = True

        btw_state = _BtwSession()

        q_short = question if len(question) <= 55 else question[:52] + "…"
        # Show a transient "btw thinking" note while the request is in flight.
        indicator = Static(
            Text(f"↩ btw: {q_short}…", style="dim italic"),
            classes="logline",
        )
        self._close_tool_group()
        await self._transcript().mount(indicator)
        self._scroll_end()

        answer_parts: list[str] = []
        try:
            async for e in run_agent_stream(
                question,
                agent,
                "btw-agent",
                btw_state,
                backend=None,
                seen_message_ids=set(),
            ):
                if isinstance(e, AssistantMessage):
                    answer_parts.append(e.text)
                elif isinstance(e, (Done, Error)):
                    break
                # ToolCall / ToolResult / StatusUpdate / TextDelta — silently
                # consumed; tool activity is invisible to the user by design.
        except asyncio.CancelledError:
            indicator.remove()
            return
        except Exception as ex:  # noqa: BLE001
            indicator.update(Text(f"↩ btw failed: {ex}", style="red"))
            return

        # Replace the "thinking" indicator with the finished answer card.
        answer = "\n\n".join(answer_parts).strip() or "(no response)"
        title_q = question if len(question) <= 50 else question[:47] + "…"
        body = Static(Markdown(answer), classes="btw-body")
        card = Collapsible(body, title=f"↩ btw: {title_q}", collapsed=False)
        card.add_class("btw-card")
        await indicator.remove()
        self._close_tool_group()
        await self._transcript().mount(card)
        self._prune_transcript()
        self._scroll_end()

    def _schedule_nova_update_check(self) -> None:
        self.run_worker(self._check_nova_update(), name="nova-update-check", group="nova-updates")

    async def _check_nova_update(self, *, force: bool = False) -> None:
        import os

        from novacode_cli.updates import check_for_update, release_details

        if not force and os.environ.get("NOVA_DISABLE_UPDATE_CHECK", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            return
        status = await asyncio.to_thread(check_for_update, force=force)
        if status.available:
            if force or getattr(self, "_notified_nova_update", None) != status.latest:
                from novacode_cli.tui.update_notice import UpdateNotice

                status = await asyncio.to_thread(release_details, status)
                # Another check may have finished while release notes were loading.
                if not force and getattr(self, "_notified_nova_update", None) == status.latest:
                    return
                self._notified_nova_update = status.latest
                release = status.release_title or f"NovaCode update ({status.latest[:12]})"
                message = f"{release} is available. View its changelog below or use /update."
                await self._transcript().mount(UpdateNotice(status))
                self._prune_transcript()
                self._scroll_end()
                self.notify(message, title="Nova update", timeout=12)
        elif force:
            self._log(
                Text(
                    f"Could not check for updates: {status.error}"
                    if status.error
                    else "Nova is up to date.",
                    style="yellow" if status.error else "dim",
                )
            )

    async def _run_update_check(self, _text: str) -> None:
        from novacode_cli.tui.screens import UpdateScreen

        self.push_screen(UpdateScreen())

    async def _run_compact(self, text: str) -> None:
        """Compact the conversation natively (spinner + result component)."""
        from novacode_cli.compaction import compact_conversation
        from novacode_cli.config.model_create import create_model

        parts = text.split(maxsplit=1)
        focus = parts[1].strip() if len(parts) > 1 else None
        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("compacting…")
        try:
            # Summarize with the SESSION's live model (honors an in-session /model
            # switch), falling back to config only if none is set.
            model = getattr(self.session_state, "_model", None) or create_model()
            # Resolve the agent dir so durable learnings can be persisted to memory.
            agent_dir = None
            try:
                from novacode_cli.config.config import settings

                if getattr(self, "assistant_id", None):
                    agent_dir = settings.get_agent_dir(self.assistant_id)
            except Exception:  # noqa: BLE001 — persistence is best-effort
                agent_dir = None
            result = await compact_conversation(
                agent=self.agent,
                model=model,
                thread_id=self.session_state.thread_id,
                focus_instructions=focus,
                # The tracker's effective window (accounts for Ollama num_ctx) so
                # the summarizer input is budgeted to what this model can accept.
                context_window=getattr(self.token_tracker, "context_window_size", None),
                agent_dir=agent_dir,
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/compact failed: {ex}", style="red"))
            return
        finally:
            self._turn_active = False
            self._set_status("ready")
        if getattr(result, "success", False):
            if self.token_tracker is not None:
                try:
                    # reset() clears the stale pre-compaction peak; recompute the
                    # breakdown from the actual post-compaction messages so ctx%
                    # is accurate immediately (not just after the next turn).
                    self.token_tracker.reset()
                    await self._update_context_breakdown()
                except Exception:  # noqa: BLE001
                    pass
                self._refresh_status()
            # A compaction is a one-off housekeeping event, not conversation
            # content: it collapses to a single line so it never buries the
            # transcript, and expands on demand for the stats and the summary.
            learnings = getattr(result, "learnings", "") or ""
            summary = getattr(result, "summary", "") or ""
            body = Text()
            body.append(
                f"messages: {result.messages_before} → {result.messages_after}\n",
                style="dim",
            )
            body.append(f"tokens saved: ~{result.tokens_saved:,}\n", style="dim")
            if learnings:
                # `learnings` is the summary text itself, written as ONE memory
                # entry — counting its newlines reported "42 learnings" for a
                # 42-line summary, which overstated what was saved.
                body.append("🧠 summary preserved to memory\n", style="green")
            if summary:
                body.append(
                    "\n" + summary[:400] + ("…" if len(summary) > 400 else ""),
                    style="dim italic",
                )
            card = Collapsible(
                Static(body, classes="compact-body"),
                title=(
                    f"● Conversation compacted  ·  "
                    f"{result.messages_before} → {result.messages_after} msgs  ·  "
                    f"~{result.tokens_saved:,} tokens saved"
                ),
                collapsed=True,
            )
            card.add_class("compact-card")
            self._close_tool_group()
            await self._transcript().mount(card)
            self._prune_transcript()
            self._scroll_end(force=False)
        else:
            self._log(
                Text(
                    f"Compaction skipped: {getattr(result, 'error', '') or 'unknown'}",
                    style="yellow",
                )
            )

    async def _run_save(self) -> None:
        """Manually persist the session (native confirmation)."""
        if self.session_manager is None:
            self._log(Text("Session saving is unavailable.", style="yellow"))
            return
        await self._save_session()
        sid = str(getattr(self.session_state, "session_id", "") or "")[:8]
        self._log(
            Text(
                f"● Session saved — resume with:  nova --continue {sid}",
                style="green",
            )
        )

    async def _run_clear(self) -> None:
        """Start a fresh chat — a total reset. Save the current conversation first.

        Clearing only the transcript would leave the agent's full history in the
        checkpointer (same thread_id), so it would still "remember" everything.
        A real reset assigns a new thread_id + session_id (fresh checkpointer
        state) AND clears every piece of carried-over context — todos, steering
        instructions, and plan mode — then drops per-conversation UI/tracking
        state and re-baselines token usage. Long-term memory, the Nova learning
        store, and the agent itself are preserved. The previous conversation is
        saved first so nothing is lost.
        """
        saved = self.session_manager is not None
        # Preserve the current conversation under its existing id, but mark it
        # cleared so neither --continue nor the --resume picker brings it back.
        await self._save_session(cleared=True)
        # Distil this conversation's un-reviewed work before it's gone.
        await self._consolidate_learning()
        # Belt-and-suspenders: explicitly mark the session cleared even if the
        # save above early-returned (e.g. the checkpointer read timed out or had
        # no messages) but a prior /save had already written it as not-cleared.
        if self.session_manager is not None:
            try:
                self.session_manager.mark_cleared(self.session_state.session_id)
            except Exception:  # noqa: BLE001
                pass

        # Total reset of session/conversation state: new thread+session id
        # (empty checkpointer state), cleared todos / steering / plan mode.
        self.session_state.reset_conversation()

        self._refresh_session_identity()

        # Re-own the live sandbox to the new session so resume reconnects to it
        # and the orphan sweep never reclaims a container the new chat still uses
        # (the container's Docker label is immutable; the registry is the source
        # of truth for ownership).
        if self._sandbox_id:
            try:
                from novacode_cli.integrations import sandbox_registry

                sandbox_registry.retie(self._sandbox_id, self.session_state.session_id)
            except Exception:  # noqa: BLE001
                pass

        # Drop per-conversation UI/tracking state.
        self._reset_streaming()
        self._routed_model_name = None
        self._refresh_info_bar()
        self._reset_subagents()
        self._paint_subagents()
        self._clear_live_steers()
        self._todos = []
        self._todos_agent = None
        self._paint_todos(None)
        self._seen.clear()
        self._restored_messages = []
        # Discard anything queued for the (now-cleared) conversation.
        self._deferred_commands.clear()
        self._deferred_prompts.clear()
        # Stale background-task completion notes must not bleed into the new chat.
        self._pending_job_notes.clear()
        # Attached images are conversation context (re-attached to every turn) —
        # clear them so the fresh chat doesn't inherit the old conversation's images.
        if self.image_tracker is not None:
            try:
                self.image_tracker.clear()
            except Exception:  # noqa: BLE001
                pass

        await self._transcript().remove_children()

        # Refresh the home screen: re-show the ASCII-art banner so /clear looks
        # like a fresh launch, not just an empty transcript.
        self._show_home_banner()

        # Re-baseline context/token accounting for the fresh chat.
        if self.token_tracker is not None:
            try:
                self.token_tracker.reset(reset_session=True)
            except Exception:  # noqa: BLE001
                pass
        # Plan/steer were cleared above — refresh the input badge to match.
        self._update_mode_badge()
        self._refresh_status()

        self._log(
            Text(
                "● Started a new chat" + (" — previous conversation saved." if saved else "."),
                style="green",
            )
        )

    def _show_home_banner(self) -> None:
        """Render the home banner: the NOVA ASCII logo composited over the rain.

        The ASCII art (from ``config.get_responsive_ascii``, sized to the live
        terminal width) is stamped on top of the Matrix rain inside a single
        :class:`MatrixRain` widget, so the rain falls *behind* the logo and the
        logo is tinted with the active TUI theme color.
        """
        try:
            from novacode_cli.config.config import get_responsive_ascii

            try:
                width = self.size.width or None
            except Exception:  # noqa: BLE001
                width = None
            art = get_responsive_ascii(width=width)

            rain = MatrixRain(
                art=art,
                width=width,
                animate=self._matrix_rain_enabled(),
                fps=min(15, self._animation_fps)
                if self._low_resource_mode
                else self._animation_fps,
            )
            self._home_banner = rain
            self._transcript().mount(rain)
            self._prune_transcript()
        except Exception:  # noqa: BLE001
            self._home_banner = None

    def on_resize(self, event: events.Resize) -> None:
        """Reflow the home banner and apply responsive breakpoints on resize.

        The rain grid width and the ASCII-art size variant are chosen from the
        terminal width, so on resize we re-pick the art variant and re-grid the
        rain. Only acts while the banner is still on screen (home screen).
        """
        self._apply_responsive_layout(event)
        rain = self._home_banner
        if not isinstance(rain, MatrixRain) or not rain.is_mounted:
            return
        try:
            from novacode_cli.config.config import get_responsive_ascii

            size = getattr(event, "size", None)
            width = (size.width if size else self.size.width) or None
            rain.reflow(get_responsive_ascii(width=width), width)
        except Exception:  # noqa: BLE001
            pass

    def on_screen_resume(self, event: Any) -> None:
        """Apply current terminal breakpoints when opening or returning from a modal."""
        self.call_after_refresh(self._apply_responsive_layout)

    def _apply_responsive_layout(self, event: events.Resize | None = None) -> None:
        """Toggle breakpoint classes from the terminal size.

        - ``narrow`` and ``compact`` progressively shed footer columns.
        - ``short`` reduces optional dock heights and frees footer rows.
        - ``tiny`` (below the _MIN_* floor): hide nonessential chrome.
          one-shot notice rather than render a broken, clipped screen.

        Only repaints when a breakpoint actually flips, so a drag-resize that
        stays in one band costs nothing extra.
        """
        size = getattr(event, "size", None) or self.size
        width = size.width or 0
        height = size.height or 0
        narrow, compact, short, tiny = _responsive_breakpoints(width, height)
        changed = not (
            narrow == self._narrow
            and compact == self._compact
            and short == self._short
            and tiny == self._tiny
        )
        self._narrow = narrow
        self._compact = compact
        self._short = short
        self._tiny = tiny
        for target in (getattr(self, "_root_screen", None), self.screen):
            if target is None:
                continue
            with suppress(Exception):
                target.set_class(narrow, "narrow")
                target.set_class(compact, "compact")
                target.set_class(short, "short")
                target.set_class(tiny, "tiny")
        if not changed:
            return
        # Status line's right-side counts are baked into a Text (not a widget),
        # so CSS can't hide them — rebuild the tail to add/drop them.
        self._status_tail = None
        self._refresh_status()
        if tiny:
            self._set_nova_indicator(
                "⚠ terminal too small — enlarge the window", style="yellow", auto_clear=4.0
            )

    async def _run_learning(self, text: str) -> None:
        """Handle /learning natively: toggle the Hermes autonomous-learning loop.

        The loop is off by default and its middleware's ``enabled`` flag is baked
        at agent-build time, so turning it on/off rebuilds the agent (same model)
        to apply the change to the running session — mirroring /effort.
        """
        from novacode_cli.config.model_create import create_model
        from novacode_cli.config.nova_config import NovaConfig

        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        cfg = NovaConfig()
        current = cfg.get_learning_enabled()

        if arg in ("", "status"):
            t = Text()
            t.append("Nova Learning (Hermes)\n", style="bold")
            t.append("Status: ", style="dim")
            t.append(
                "on\n" if current else "off\n", style="bold green" if current else "bold yellow"
            )
            t.append(
                "\nWhen on, Nova periodically self-reviews its tool usage, extracts\n"
                "lessons to memory, and creates/refines skills as you work.\n",
                style="dim",
            )
            t.append("Usage: /learning <on|off>\n", style="dim")
            self._log(t)
            return

        if arg not in ("on", "off"):
            self._log(
                Text(f"Invalid option '{arg}'. Usage: /learning <on|off|status>", style="red")
            )
            return

        want = arg == "on"
        if want == current:
            self._log(Text(f"Nova learning already {arg}.", style="dim"))
            return

        cfg.set_learning_enabled(want)
        t = Text(
            f"● Nova learning {'enabled' if want else 'disabled'} and saved to config.\n",
            style="green",
        )

        applied = False
        if self.session_state is not None:
            try:
                new_agent, new_backend = await self.session_state.switch_model(create_model())
                self.agent = new_agent
                self.backend = new_backend
                applied = True
            except Exception as e:  # noqa: BLE001
                t.append(f"⚠ Could not apply to the running session: {e}\n", style="yellow")

        if applied:
            t.append(
                "● Applied to this session." if want else "● Stopped for this session.",
                style="green",
            )
        else:
            t.append("Takes effect on restart.", style="dim")

        self._log(t)
        self._refresh_status()

    async def _run_effort(self, text: str) -> None:
        """Handle /effort natively: set reasoning effort level."""
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli.config.model_create import create_model

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        nova_config = NovaConfig()
        current = nova_config.get("reasoning_effort", "off")

        if not args:
            t = Text()
            t.append("Reasoning Effort Configuration\n", style="bold")
            t.append(f"Current: ", style="dim")
            t.append(f"{current}\n", style="bold cyan")
            t.append("\nUsage: /effort <low|medium|high|off>\n", style="dim")
            self._log(t)
            return

        val = args.strip().lower()
        if val in ("none", "default"):
            val = "off"
        if val not in ("low", "medium", "high", "off"):
            self._log(
                Text(f"Invalid effort level '{args}'. Choose: low, medium, high, off", style="red")
            )
            return

        nova_config.set("reasoning_effort", val)
        t = Text(f"● Reasoning effort set to '{val}' and saved to config.\n", style="green")

        # Hot-swap the model
        if self.session_state is not None:
            try:
                new_model = create_model()
                new_agent, new_backend = await self.session_state.switch_model(new_model)
                self.agent = new_agent
                self.backend = new_backend
                self.model_name = getattr(new_model, "model_name", None) or getattr(
                    new_model, "model", "unknown"
                )
                if self.token_tracker is not None:
                    try:
                        self.token_tracker.set_model(self.model_name)
                    except Exception:  # noqa: BLE001
                        pass
                t.append("● Model recreated with new reasoning effort dynamically!", style="green")
            except Exception as e:
                t.append(f"⚠ Could not recreate model dynamically: {e}", style="yellow")
                t.append(
                    "\nThe change will take effect on next model switch or restart.", style="dim"
                )
        else:
            t.append("The change will take effect on restart.", style="dim")

        self._log(t)
        self._refresh_info_bar()  # the model may have been recreated — refresh footer

    async def _run_steer(self, text: str) -> None:
        """Manage persistent steering instructions natively (add/list/clear/remove)."""
        import re

        from novacode_cli.bootstrap.steering import (
            SteeringInstruction,
            classify_instruction,
        )

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        if getattr(self.session_state, "steering_instructions", None) is None:
            self.session_state.steering_instructions = []
        instr = self.session_state.steering_instructions
        low = args.lower()

        if not args or low in ("list", "ls", "show"):
            t = Text()
            t.append("Steering instructions\n", style="bold")
            if not instr:
                t.append("  (none — /steer <instruction> to add)\n", style="dim")
            else:
                for i, si in enumerate(instr, 1):
                    t.append(f"  {i}. ", style="cyan")
                    t.append(f"{si.label}: {si.instruction}\n", style="dim")
            self._log(t)
            return
        if low in ("clear", "reset"):
            n = len(instr)
            instr.clear()
            self._log(Text(f"Cleared {n} steering instruction(s).", style="green"))
            return
        m = re.match(r"(?:remove|rm|del|delete)\s+(\d+)", args, re.IGNORECASE)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(instr):
                removed = instr.pop(idx)
                self._log(Text(f"Removed: {removed.label}", style="green"))
            else:
                self._log(Text(f"Invalid index {idx + 1}.", style="yellow"))
            return
        label = classify_instruction(args)
        instr.append(SteeringInstruction(label=label, instruction=args))
        self._log(
            Text(
                f"● Added steering [{label}]: {args}\n"
                f"  {len(instr)} active — injected into every turn.",
                style="green",
            )
        )

    async def _run_notifications(self, text: str) -> None:
        """Native /notifications: list, dismiss <id>, approve <id>, or clear."""
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        ap = args.split(maxsplit=1)
        sub = ap[0].lower() if ap else ""
        ss = self.session_state

        if sub in ("clear", "reset"):
            # Dismiss all — reject any pending approvals.
            for n in list(ss.notifications):
                if n.action_id and not n.dismissed:
                    ss.resolve_approval(n.action_id, approve=False)
            n = ss.clear_notifications()
            self._log(Text(f"Cleared {n} notification(s).", style="green"))
            self._refresh_status()
            return
        if sub in ("dismiss", "rm", "ack") and len(ap) > 1:
            nid = ap[1].strip()
            n = self._find_notification(ss, nid)
            if n and n.action_id and not n.dismissed:
                ss.resolve_approval(n.action_id, approve=False)
                self._log(Text(f"Dismissed {nid} (rejected approval).", style="green"))
            elif ss.dismiss_notification(nid):
                self._log(Text(f"Dismissed {nid}", style="green"))
            else:
                self._log(Text(f"Notification {nid} not found", style="yellow"))
            self._refresh_status()
            return
        if sub == "approve" and len(ap) > 1:
            nid = ap[1].strip()
            n = self._find_notification(ss, nid)
            if n and n.action_id and not n.dismissed:
                if ss.resolve_approval(n.action_id, approve=True):
                    self._log(Text(f"Approved {nid}.", style="green"))
                else:
                    self._log(Text(f"Approval {nid} already resolved.", style="yellow"))
            else:
                self._log(
                    Text(
                        f"Notification {nid} not found or has no pending approval.",
                        style="yellow",
                    )
                )
            self._refresh_status()
            return

        notes = list(ss.notifications)
        colors = {
            "info": "cyan",
            "success": "green",
            "warning": "yellow",
            "error": "red",
            "approval": "magenta",
        }
        pending = ss.pending_approval_count()
        title = f"Notifications ({ss.unread_notification_count()} unread"
        if pending:
            title += f", {pending} pending approval"
        title += ")"
        t = Text()
        t.append(f"{title}\n", style="bold")
        if not notes:
            t.append("  (none yet — long-running tasks notify here)\n", style="dim")
        else:
            for n in notes:
                c = colors.get(n.level, "white")
                marker = "●" if not n.dismissed else "○"
                if n.action_id and not n.dismissed and n.action_type == "approve":
                    marker = f"⚡{marker}"
                t.append(f"  {marker} ", style=c)
                t.append(f"{n.id} ", style="dim")
                t.append(f"{n.timestamp.strftime('%H:%M:%S')} ", style="dim")
                t.append(f"[{n.source}] ", style="dim")
                t.append(f"{n.title}", style=c)
                if n.message:
                    t.append(f" — {n.message[:60]}", style="dim")
                t.append("\n")
            t.append(
                "  /notifications dismiss <id> · /notifications approve <id>"
                " · /notifications clear\n",
                style="dim",
            )
        self._log(t)

    @staticmethod
    def _find_notification(ss, nid: str) -> object | None:
        """Return the Notification with the given id, or None."""
        for n in ss.notifications:
            if n.id == nid:
                return n
        return None

    async def _tui_execute_fn(
        self,
        user_input,
        agent=None,
        assistant_id=None,
        session_state=None,
        token_tracker=None,
        backend=None,
        is_subagent=False,
        image_tracker=None,
        seen_message_ids=None,
        *,
        skip_file_mentions=False,
    ) -> None:
        await self._stream_prompt(user_input, assistant_id=assistant_id)

    async def _tui_quiet_execute_fn(
        self,
        user_input,
        agent=None,
        assistant_id=None,
        session_state=None,
        token_tracker=None,
        backend=None,
        is_subagent=False,
        image_tracker=None,
        seen_message_ids=None,
        *,
        skip_file_mentions=False,
    ) -> None:
        """Execute the agent run quietly without streaming events to the TUI transcript.

        This avoids event loop flooding and unresponsiveness during intensive
        background operations like /init.
        """
        from novacode_cli.agent_stream import run_agent_stream
        from novacode_cli.ui_events import InterruptRequest, StatusUpdate

        ag = agent or self._init_agent or self.agent
        aid = assistant_id or self.assistant_id

        async for e in run_agent_stream(
            user_input,
            ag,
            aid,
            self.session_state,
            backend=backend or self._init_backend or self.backend,
            image_tracker=self.image_tracker,
            seen_message_ids=self._seen,
        ):
            if isinstance(e, StatusUpdate):
                self._set_status(e.message or "ready")
            elif isinstance(e, InterruptRequest):
                await self._handle_interrupt(e)

    async def _run_research(self, text: str) -> None:
        """Launch the research swarm, streaming the run as native widgets."""
        from novacode_cli.commands.research_handler import handle_research_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        if not args:
            # No query → let the handler emit its usage block, surface natively.
            with _rich_console.capture() as cap:
                await handle_research_command(
                    self.agent, self.session_state, self.token_tracker, cmd_args=None
                )
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /research <query>", style="dim"))
            return
        self._log(Text(f"🔬 Research: {args}", style="bold"))
        # Capture (and discard) the handler's setup prints; the agent run itself
        # streams natively through _tui_execute_fn → _stream_prompt.
        with _rich_console.capture():
            await handle_research_command(
                self.agent,
                self.session_state,
                self.token_tracker,
                cmd_args=args,
                execute_fn=self._tui_execute_fn,
            )

    async def _run_ingest(self, text: str) -> None:
        """Ingest a raw source into the wiki."""
        from novacode_cli.commands.wiki_commands import handle_ingest

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        if not args:
            # Show usage
            with _rich_console.capture() as cap:
                from novacode_cli.commands import CommandContext

                mock_ctx = CommandContext(
                    cmd="ingest",
                    cmd_args=None,
                    agent=self.agent,
                    token_tracker=self.token_tracker,
                    session_state=self.session_state,
                    assistant_id=self.assistant_id,
                )
                await handle_ingest(mock_ctx)
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /ingest <raw_path>", style="dim"))
            return
        self._log(Text(f"📥 Ingesting: {args}", style="bold"))
        with _rich_console.capture() as cap:
            from novacode_cli.commands import CommandContext

            mock_ctx = CommandContext(
                cmd="ingest",
                cmd_args=args,
                agent=self.agent,
                token_tracker=self.token_tracker,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
            )
            await handle_ingest(mock_ctx, execute_fn=self._tui_execute_fn)
        out = cap.get().strip()
        if out:
            self._log(Text.from_ansi(out))

    async def _run_ask(self, text: str) -> None:
        """Ask a question with wiki context prepended."""
        from novacode_cli.commands.wiki_commands import handle_ask

        parts = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""
        if not question:
            with _rich_console.capture() as cap:
                from novacode_cli.commands import CommandContext

                mock_ctx = CommandContext(
                    cmd="ask",
                    cmd_args=None,
                    agent=self.agent,
                    token_tracker=self.token_tracker,
                    session_state=self.session_state,
                    assistant_id=self.assistant_id,
                )
                await handle_ask(mock_ctx)
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /ask <question>", style="dim"))
            return
        self._log(Text(f"📚 Asking: {question}", style="bold"))
        with _rich_console.capture() as cap:
            from novacode_cli.commands import CommandContext

            mock_ctx = CommandContext(
                cmd="ask",
                cmd_args=question,
                agent=self.agent,
                token_tracker=self.token_tracker,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
            )
            await handle_ask(mock_ctx, execute_fn=self._tui_execute_fn)
        out = cap.get().strip()
        if out:
            self._log(Text.from_ansi(out))

    async def _run_file(self, text: str) -> None:
        """File conversation knowledge into the wiki."""
        from novacode_cli.commands.wiki_commands import handle_file

        parts = text.split(maxsplit=1)
        topic = parts[1].strip() if len(parts) > 1 else ""
        if not topic:
            with _rich_console.capture() as cap:
                from novacode_cli.commands import CommandContext

                mock_ctx = CommandContext(
                    cmd="file",
                    cmd_args=None,
                    agent=self.agent,
                    token_tracker=self.token_tracker,
                    session_state=self.session_state,
                    assistant_id=self.assistant_id,
                )
                await handle_file(mock_ctx)
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /file <topic>", style="dim"))
            return
        self._log(Text(f"📝 Filing: {topic}", style="bold"))
        with _rich_console.capture() as cap:
            from novacode_cli.commands import CommandContext

            mock_ctx = CommandContext(
                cmd="file",
                cmd_args=topic,
                agent=self.agent,
                token_tracker=self.token_tracker,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
            )
            await handle_file(mock_ctx, execute_fn=self._tui_execute_fn)
        out = cap.get().strip()
        if out:
            self._log(Text.from_ansi(out))

    async def _run_wiki(self) -> None:
        """Show the Obsidian LLM Wiki browser (interactive)."""
        await self.push_screen_wait(WikiScreen())

    async def _run_dream(self) -> None:
        """Run /dream: show a native memory-consolidation summary, then stream it."""
        from novacode_cli.commands.dream_handler import handle_dream_command

        # Collect the handler's status lines and render them as ONE cohesive
        # native block (blank separators are dropped — no empty log widgets).
        status_lines: list[str] = []

        def _emit(message: str = "") -> None:
            if message:
                status_lines.append(message)

        result = await handle_dream_command(self.session_state, self.assistant_id, emit=_emit)

        if status_lines:
            block = Text()
            for i, line in enumerate(status_lines):
                try:
                    block.append_text(Text.from_markup(line))
                except Exception:  # noqa: BLE001 - bad markup: show literally
                    block.append(line)
                if i < len(status_lines) - 1:
                    block.append("\n")
            self._log(block)

        if isinstance(result, str) and result.strip():
            self._log(Text("💭 Dreaming over memories…", style="bold"))
            await self._stream_prompt(result)

    async def _run_evolution(self) -> None:
        """Open the native evolution dashboard without tying up the turn."""
        from novacode_cli.tui.reference_screens import EvolutionScreen

        self.push_screen(EvolutionScreen())

    async def _run_reindex(self) -> None:
        """Rebuild the semantic code-search index, with a native status."""
        try:
            from novacode_cli.tools.code_search_tools import (
                _get_index,
                _is_semble_available,
                _reset_index,
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Code search unavailable: {ex}", style="yellow"))
            return
        if not _is_semble_available():
            self._log(
                Text(
                    "Code search is not available. Install 'semble' to enable "
                    "semantic code search (pip install semble).",
                    style="yellow",
                )
            )
            return

        from novacode_cli.config.config import settings as _settings

        workspace = _settings.get_workspace_root()
        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("re-indexing…")
        try:
            _reset_index()
            idx = await asyncio.to_thread(_get_index, workspace)
            if idx is not None:
                self._log(Text(f"● Code search index rebuilt for {workspace}", style="green"))
            else:
                self._log(Text("Failed to build code search index.", style="red"))
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Reindex failed: {ex}", style="red"))
        finally:
            self._turn_active = False
            self._set_status("ready")

    async def _run_images(self, text: str) -> None:
        """Native /images: list, remove, or clear conversation images."""
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        it = self.image_tracker
        if it is None:
            self._log(Text("Image tracking not available.", style="yellow"))
            return
        arg_parts = args.split(maxsplit=1)
        sub = arg_parts[0].lower() if arg_parts else ""

        if not args or sub == "list":
            images = it.list_images()
            t = Text()
            t.append("Images in conversation\n", style="bold")
            if not images:
                t.append(
                    "  (none — paste with Ctrl+V or reference @path/to/image.png)\n",
                    style="dim",
                )
            else:
                for img in images:
                    t.append(f"  {img['id']}", style="cyan")
                    t.append(
                        f"  {img['format'].upper()}  {img['size_kb']:.1f} KB  "
                        f"{img['placeholder']}\n",
                        style="dim",
                    )
                t.append(
                    f"  Total: {len(images)} — /images remove <id> · /images clear\n",
                    style="dim",
                )
            self._log(t)
            return
        if sub == "remove":
            if len(arg_parts) < 2:
                self._log(Text("Usage: /images remove <id>", style="red"))
                return
            image_id = arg_parts[1].strip()
            if not image_id.startswith("image-"):
                image_id = f"image-{image_id}"
            if it.remove_image(image_id):
                self._log(Text(f"Removed {image_id}", style="green"))
            else:
                avail = ", ".join(i["id"] for i in it.list_images())
                msg = f"Image not found: {image_id}"
                if avail:
                    msg += f" (available: {avail})"
                self._log(Text(msg, style="red"))
            return
        if sub == "clear":
            count = it.count
            if count == 0:
                self._log(Text("No images to clear.", style="dim"))
            else:
                it.clear()
                self._log(Text(f"Cleared {count} image(s) from conversation.", style="green"))
            return
        self._log(Text("Usage: /images [list | remove <id> | clear]", style="red"))

    async def _run_files(self) -> None:
        """Native /files: session read/write summary from the file tracker."""
        from novacode_cli.tracking.file_tracker import get_session_tracker

        tr = get_session_tracker()
        t = Text()
        t.append("Session file operations\n", style="bold")
        t.append(f"  read: {len(tr.files_read)} files / {tr.total_reads} ops\n", style="dim")
        t.append(
            f"  modified: {len(tr.files_written)} files / {tr.total_writes} ops\n",
            style="dim",
        )
        if getattr(tr, "rejected_edits", 0):
            t.append(f"  rejected edits (unread files): {tr.rejected_edits}\n", style="red")
        if tr.files_read:
            t.append("\nRecently read\n", style="bold")
            for path in tr.read_order[-15:]:
                rec = tr.files_read[path]
                disp = path if len(path) <= 60 else "..." + path[-57:]
                t.append(f"  {disp}", style="cyan")
                t.append(f"  ({rec.line_count} lines)\n", style="dim")
        if tr.files_written:
            t.append("\nRecently modified\n", style="bold")
            for path in tr.write_order[-15:]:
                recs = tr.files_written[path]
                disp = path if len(path) <= 60 else "..." + path[-57:]
                ops = ", ".join(r.operation for r in recs[-3:])
                if len(recs) > 3:
                    ops = f"({len(recs)}x) " + ops
                t.append(f"  {disp}", style="yellow")
                t.append(f"  {ops}\n", style="dim")
        self._log(t)

    async def _run_tests(self, text: str) -> None:
        """Native /tests: detect framework (or use args) and stream results."""
        import threading

        from novacode_cli.server_runner.test_runner import (
            detect_test_framework,
            get_default_test_command,
            run_tests,
        )

        parts = text.split(maxsplit=1)
        cmd_args = parts[1].strip() if len(parts) > 1 else ""
        from novacode_cli.config.config import settings

        working_dir = str(settings.get_workspace_root())
        if not cmd_args:
            framework = detect_test_framework(working_dir)
            command = get_default_test_command(framework)
            if not command:
                self._log(
                    Text(
                        "Could not auto-detect test framework. "
                        "Specify one: /tests pytest  or  /tests npm test",
                        style="yellow",
                    )
                )
                return
            self._log(Text(f"Detected {framework.value} — running: {command}", style="dim"))
        else:
            command = cmd_args
            self._log(Text(f"Running: {command}", style="dim"))

        loop_tid = threading.get_ident()

        def _cb(line: str) -> None:
            if threading.get_ident() == loop_tid:
                self._log(Text(line, style="dim"))
            else:
                try:
                    self.call_from_thread(self._log, Text(line, style="dim"))
                except Exception:  # noqa: BLE001
                    pass

        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("running tests…")
        try:
            result = await run_tests(command=command, working_dir=working_dir, output_callback=_cb)
            t = Text()
            t.append(
                "● Tests passed\n" if result.success else "✖ Tests failed\n",
                style="green" if result.success else "red",
            )
            stats = []
            if result.tests_run is not None:
                stats.append(f"{result.tests_run} run")
            if result.tests_passed is not None:
                stats.append(f"{result.tests_passed} passed")
            if result.tests_failed is not None:
                stats.append(f"{result.tests_failed} failed")
            if result.duration_seconds is not None:
                stats.append(f"{result.duration_seconds:.2f}s")
            if stats:
                t.append("  " + ", ".join(stats) + "\n", style="dim")
            if result.error:
                t.append(f"  error: {result.error}\n", style="red")
            self._log(t)
            self._notify_test_result(result)
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Test run failed: {ex}", style="red"))
        finally:
            self._turn_active = False
            self._set_status("ready")

    def _notify_test_result(self, result) -> None:
        """Record a notification summarizing a finished test run."""
        try:
            ok = bool(getattr(result, "success", False))
            passed = getattr(result, "tests_passed", None)
            failed = getattr(result, "tests_failed", None)
            total = getattr(result, "tests_run", None)
            dur = getattr(result, "duration_seconds", None)
            if passed is not None and total:
                title = f"Tests: {passed}/{total} passed"
            else:
                title = "Tests passed" if ok else "Tests failed"
            msg = f"{failed} failed" if failed is not None else ("ok" if ok else "failed")
            if dur is not None:
                msg += f" · {dur:.1f}s"
            self.session_state.add_notification(
                level="success" if ok else "error",
                title=title,
                message=msg,
                source="tests",
            )
        except Exception:  # noqa: BLE001
            pass

    async def _run_servers(self) -> None:
        """Show running servers (interactive)."""
        await self.push_screen_wait(ServersScreen())

    async def _run_kill(self, text: str) -> None:
        """Native /kill: kill a process by PID/name (arg) or via a picker."""
        from novacode_cli.process_manager import ProcessManager

        manager = ProcessManager.get_instance()
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg:
            try:
                pid = int(arg)
                ok = await manager.stop_process(pid)
                self._log(
                    Text(
                        (f"● Killed process {pid}" if ok else f"No process with PID {pid}"),
                        style="green" if ok else "yellow",
                    )
                )
                return
            except ValueError:
                pass
            ok = await manager.stop_by_name(arg)
            self._log(
                Text(
                    f"● Killed process '{arg}'" if ok else f"No process named '{arg}'",
                    style="green" if ok else "yellow",
                )
            )
            return

        processes = manager.list_processes(alive_only=True)
        if not processes:
            self._log(Text("No managed processes running.", style="yellow"))
            return
        opts = [f"[{p.pid}] {p.name}" + (f" (port {p.port})" if p.port else "") for p in processes]
        idx = await self.push_screen_wait(PickScreen("Kill which process?", opts))
        if 0 <= idx < len(processes):
            info = processes[idx]
            ok = await manager.stop_process(info.pid)
            self._log(
                Text(
                    (
                        f"● Killed '{info.name}' (PID {info.pid})"
                        if ok
                        else "Failed to kill process"
                    ),
                    style="green" if ok else "red",
                )
            )

    async def _run_restore(self, text: str) -> None:
        """Native /restore: restore a file snapshot by arg or via a picker."""
        from datetime import datetime

        from novacode_cli.recovery import REASON_LABELS, get_recovery_manager

        mgr = get_recovery_manager()
        if mgr is None:
            self._log(Text("No recovery manager active for this session.", style="yellow"))
            return
        snapshots = mgr.list_snapshots(include_past_sessions=True)
        if not snapshots:
            self._log(
                Text(
                    "No file snapshots found. Snapshots are created before "
                    "rm/write_file/edit_file.",
                    style="yellow",
                )
            )
            return

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        def _restore(idx: int) -> None:
            session_id, entry = snapshots[idx]
            ok = mgr.restore(entry, session_id=session_id)
            self._log(
                Text(
                    (
                        f"● Restored {entry.original_path}"
                        if ok
                        else f"Failed to restore {entry.original_path}"
                    ),
                    style="green" if ok else "red",
                )
            )

        if arg:
            if arg.isdigit():
                i = int(arg) - 1
                if 0 <= i < len(snapshots):
                    _restore(i)
                else:
                    self._log(Text(f"No snapshot at index {arg}.", style="red"))
                return
            for i, (_sid, entry) in enumerate(snapshots):
                if arg in entry.original_path or entry.original_path.endswith(arg):
                    _restore(i)
                    return
            self._log(Text(f"No snapshot matching '{arg}'.", style="red"))
            return

        now = datetime.now()
        opts = []
        for _sid, entry in snapshots:
            label = REASON_LABELS.get(entry.reason, entry.reason)
            try:
                secs = int((now - datetime.fromisoformat(entry.timestamp)).total_seconds())
                age = (
                    f"{secs}s ago"
                    if secs < 60
                    else (
                        f"{secs // 60}m ago"
                        if secs < 3600
                        else (f"{secs // 3600}h ago" if secs < 86400 else f"{secs // 86400}d ago")
                    )
                )
            except Exception:  # noqa: BLE001
                age = entry.timestamp
            opts.append(f"{entry.original_path}  — {label} ({age})")
        idx = await self.push_screen_wait(PickScreen("Restore which snapshot?", opts))
        if 0 <= idx < len(snapshots):
            _restore(idx)

    async def _run_hooks(self, text: str) -> None:
        """Show hooks manager (interactive)."""
        await self.push_screen_wait(HooksScreen())

    async def _run_browser_use(self, text: str) -> None:
        """Run /browser-use; the agent analysis streams natively via execute_fn."""
        from novacode_cli.commands.browser_use_handler import handle_browser_use_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        self._log(Text(f"🌐 Browser task: {args or '(status)'}", style="bold"))
        # Browser automation + setup prints are captured/discarded; the follow-up
        # agent run renders natively through _tui_execute_fn.
        with _rich_console.capture():
            await handle_browser_use_command(
                self.agent,
                self.session_state,
                self.assistant_id,
                self.token_tracker,
                args or None,
                execute_fn=self._tui_execute_fn,
            )

    # --- /ralph native widgets -------------------------------------------
    _RALPH_ITER_GLYPH = {
        "running": ("▶", "yellow"),
        "done": ("●", "green"),
        "failed": ("✖", "red"),
    }

    def _ralph_mount(self, widget: Widget) -> None:
        """Mount a native Ralph card into the transcript (app thread only)."""
        self._close_tool_group()
        self._transcript().mount(widget)
        self._prune_transcript()
        self._scroll_end()

    def _ralph_run_text(self, event: Any) -> Text:
        """Header card for a Ralph run: task, iteration budget, and mode."""
        iters = "unlimited" if event.max_iterations == 0 else str(event.max_iterations)
        title = "🔁 Ralph Mode (Resumed)" if event.resumed_from else "🔁 Ralph Mode"
        t = Text()
        t.append(f"{title}\n", style="bold")
        t.append("Task: ", style="bold")
        t.append(f"{event.task}\n")
        t.append("Max iterations: ", style="bold")
        t.append(f"{iters}\n")
        if event.resumed_from:
            t.append("Resuming from: ", style="bold")
            t.append(f"iteration {event.resumed_from}\n")
        t.append("Mode: ", style="bold")
        t.append("background (non-blocking)" if event.background else "foreground")
        return t

    def _ralph_iter_text(
        self,
        iteration: int,
        max_iterations: int,
        status: str,
        elapsed: float | None = None,
        error: str | None = None,
    ) -> Text:
        """One iteration card line, styled by ``status`` (running/done/failed)."""
        glyph, color = self._RALPH_ITER_GLYPH.get(status, ("•", "dim"))
        disp = f"{iteration}/{max_iterations}" if max_iterations > 0 else str(iteration)
        t = Text()
        t.append(f"{glyph} Iteration {disp}", style=f"bold {color}")
        if status == "running":
            t.append("  — running…", style="dim")
        else:
            t.append(f"  — {'done' if status == 'done' else 'failed'}", style=color)
            if elapsed is not None:
                t.append(f" ({elapsed:.1f}s)", style="dim")
            if error:
                t.append(f"\n    {error}", style="red")
        return t

    def _ralph_status_renderable(self, snap: Any) -> Any:
        """Render a ``/ralph --status`` snapshot as a native table card."""
        from rich.console import Group
        from rich.table import Table

        header = Text("Ralph Background Tasks\n", style="bold")
        if not snap.rows:
            header.append("No background Ralph tasks running.", style="dim")
            return header

        table = Table(show_edge=False, pad_edge=False, expand=False)
        table.add_column("", width=2)
        table.add_column("Iter")
        table.add_column("Status")
        table.add_column("Elapsed", justify="right")
        table.add_column("Task")
        glyphs = {
            "running": ("◐", self._palette.tool_pending),
            "completed": ("●", self._palette.tool_ok),
            "failed": ("✖", self._palette.tool_fail),
        }
        for row in snap.rows:
            g, color = glyphs.get(row.status, ("•", "dim"))
            disp = (
                f"{row.iteration}/{row.max_iterations}"
                if row.max_iterations > 0
                else str(row.iteration)
            )
            desc = row.task if len(row.task) <= 50 else row.task[:50] + "…"
            table.add_row(
                Text(g, style=color),
                disp,
                Text(row.status, style=color),
                f"{row.elapsed:.0f}s",
                desc,
            )
        summary = Text()
        summary.append(f"\nTotal {snap.total}", style="dim")
        summary.append(f"  ·  running {snap.running}", style="yellow")
        summary.append(f"  ·  completed {snap.completed}", style="green")
        summary.append(f"  ·  failed {snap.failed}", style="red")
        return Group(header, table, summary)

    def _ralph_on_event(self, event: Any) -> None:
        """Drive native Ralph widgets from a structured handler event (app thread).

        The UI-agnostic handler reports run milestones through
        :mod:`novacode_cli.commands.ralph_events`, and this turns each into a
        native card instead of a flat log line.
        """
        from novacode_cli.commands import ralph_events as rev

        if isinstance(event, rev.RalphStarted):
            self._ralph_iter_cards.clear()
            self._ralph_mount(Static(self._ralph_run_text(event), classes="ralph-run"))
        elif isinstance(event, rev.IterationStarted):
            card = Static(
                self._ralph_iter_text(event.iteration, event.max_iterations, "running"),
                classes="ralph-iter running",
            )
            self._ralph_iter_cards[event.iteration] = card
            self._ralph_mount(card)
        elif isinstance(event, rev.IterationFinished):
            status = "done" if event.ok else "failed"
            text = self._ralph_iter_text(
                event.iteration, event.max_iterations, status, event.elapsed, event.error
            )
            card = self._ralph_iter_cards.get(event.iteration)
            updated = False
            if card is not None:
                try:
                    card.set_classes(f"ralph-iter {status}")
                    _paint(card, text)
                    updated = True
                except Exception:  # noqa: BLE001 - card may have been pruned
                    updated = False
            if not updated:
                self._ralph_mount(Static(text, classes=f"ralph-iter {status}"))
        elif isinstance(event, rev.RalphFinished):
            t = Text()
            t.append("📊 Ralph finished", style="bold")
            t.append(f" — {event.completed} completed", style="green")
            if event.failed:
                t.append(f", {event.failed} failed", style="red")
            t.append(f" of {event.total} iteration(s)", style="dim")
            self._ralph_mount(Static(t, classes="ralph-summary"))
        elif isinstance(event, rev.StatusSnapshot):
            self._ralph_mount(Static(self._ralph_status_renderable(event), classes="ralph-status"))

    async def _run_ralph(self, text: str) -> None:
        """Run /ralph natively: structured milestones render as native cards via
        ``on_event``, free-form notices via a thread-safe ``emit``, and foreground
        iterations stream through ``_tui_execute_fn``."""
        import threading

        from novacode_cli.commands.ralph_handler import handle_ralph_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        self._log(Text(f"🔁 Ralph: {args or '(status)'}", style="bold"))

        loop_tid = threading.get_ident()

        def _emit(message: str = "") -> None:
            try:
                renderable = Text.from_markup(message) if message else Text("")
            except Exception:  # noqa: BLE001 - never let bad markup break the run
                renderable = Text(message)
            if threading.get_ident() == loop_tid:
                self._log(renderable)
            else:
                try:
                    self.call_from_thread(self._log, renderable)
                except Exception:  # noqa: BLE001 - app may be shutting down
                    pass

        def _on_event(event: Any) -> None:
            # Background runs fire events from a worker thread; hop to the app
            # thread before touching widgets (same contract as ``_emit``).
            if threading.get_ident() == loop_tid:
                self._ralph_on_event(event)
            else:
                try:
                    self.call_from_thread(self._ralph_on_event, event)
                except Exception:  # noqa: BLE001 - app may be shutting down
                    pass

        await handle_ralph_command(
            self.agent,
            self.session_state,
            self.assistant_id,
            self.token_tracker,
            args or None,
            execute_fn=self._tui_execute_fn,
            emit=_emit,
            on_event=_on_event,
        )

    async def _run_trello(self, text: str) -> None:
        """Run /trello; start the server inline, then watch for tasks in background."""
        from novacode_cli.commands.trello_handler import (
            _handle_status,
            _handle_stop,
        )
        from novacode_cli.commands.trello_server import TrelloServer

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        # Subcommands that don't need the processing loop
        if args == "stop":
            with _rich_console.capture() as cap:
                _handle_stop(self.session_state)
            self._log(Text.from_ansi(cap.get()))
            return
        if args == "status":
            with _rich_console.capture() as cap:
                _handle_status(self.session_state)
            self._log(Text.from_ansi(cap.get()))
            return

        # Check if already running
        existing_server: TrelloServer | None = getattr(self.session_state, "trello_server", None)
        if existing_server and existing_server.is_running:
            self._log(
                Text(
                    f"Trello board already running at {existing_server.url}",
                    style="yellow",
                )
            )
            return

        # Start the server
        server = TrelloServer()
        await server.start()
        from novacode_cli.commands.trello_remote import connect_telegram

        owner = self.session_state

        async def connect_board_telegram():
            bridge = await connect_telegram(owner)
            self._ensure_remote_consumer()
            return bridge

        server.bind_remote(connect_board_telegram, str(getattr(owner, "session_id", "trello")))
        self.session_state.trello_server = server
        self._log(
            Text(
                f"📋 Trello board started at {server.url}",
                style="bold green",
            )
        )
        self._log(
            Text(
                "Add tasks in the browser. The agent will process them one at a time.",
                style="dim",
            )
        )

        # Launch the processing loop as a background task so the TUI stays responsive
        from novacode_cli.utils.tasks import spawn

        spawn(
            self._trello_watch_loop(
                server,
                owner_pane=self._active_pane,
                agent=self.agent,
                assistant_id=self.assistant_id,
                session_state=owner,
                token_tracker=self.token_tracker,
            )
        )

    async def _run_create(self, text: str) -> None:
        """Run /create; start the Skills & Agents web UI server."""
        from novacode_cli.commands.create_server import CreateServer

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        # Subcommand: stop
        if args == "stop":
            server: CreateServer | None = getattr(self.session_state, "create_server", None)
            if server and server.is_running:
                server.stop()
                self.session_state.create_server = None
                self._log(Text("Create UI stopped.", style="green"))
            else:
                self._log(Text("Create UI is not running.", style="yellow"))
            return

        # Check if already running
        existing_server: CreateServer | None = getattr(self.session_state, "create_server", None)
        if existing_server and existing_server.is_running:
            self._log(
                Text(
                    f"Create UI already running at {existing_server.url}",
                    style="yellow",
                )
            )
            return

        # Start the server
        server = CreateServer()
        await server.start()
        self.session_state.create_server = server
        self._log(
            Text(
                f"Create UI started at {server.url}",
                style="bold green",
            )
        )
        self._log(
            Text(
                "Browse, preview, edit, and create skills & agents in the browser.",
                style="dim",
            )
        )

    async def _trello_watch_loop(
        self,
        server: Any,
        *,
        owner_pane=None,
        agent=None,
        assistant_id=None,
        session_state=None,
        token_tracker=None,
    ) -> None:
        """Background loop: process board tasks via the shared watch loop."""
        from novacode_cli.commands.trello_handler import trello_watch_loop

        owner_pane = owner_pane or self._active_pane
        agent = agent or self.agent
        session_state = session_state or self.session_state
        assistant_id = assistant_id or self.assistant_id
        token_tracker = token_tracker or self.token_tracker

        def _log(message: str, style: str = "") -> None:
            self._log_task_output(owner_pane, Text(message, style=style or None))

        async def execute_card(prompt, *_args):
            # Board work must not run against whichever tab happens to be
            # selected when a queued task becomes ready. Preserve the user's
            # viewport and wait for its owning tab to be available.
            while server.is_running:
                lock = getattr(session_state, "_remote_message_lock", None)
                if (
                    owner_pane is self._active_pane
                    and not self._turn_active
                    and not (lock and lock.locked())
                ):
                    await self._tui_execute_fn(prompt, assistant_id=assistant_id)
                    return
                await asyncio.sleep(0.1)
            raise asyncio.CancelledError()

        try:
            await trello_watch_loop(
                server,
                agent,
                assistant_id,
                session_state,
                token_tracker,
                execute_card,
                _log,
            )
        except Exception:
            pass  # Server stopped, loop ends

    # ── /council — collaborative planning ───────────────────────────────────
    #
    # The council plans; it never edits. A run stops at "awaiting approval" and
    # only an explicit `/council approve N` turns a plan into work for the
    # coding agent. Approval reads the run back from its artifact rather than
    # from memory, so a plan can still be approved after a restart.

    def _council_store(self):
        from novacode_cli.council_planner import CouncilArtifactStore

        return CouncilArtifactStore(Path.cwd())

    def _council_target(self, store):
        """The run a bare /council subcommand acts on: this session's, else the
        most recent on disk."""
        run_id = getattr(self, "_council_run_id", None)
        return store.load(run_id) if run_id else store.latest()

    async def _run_council(self, text: str) -> None:
        """Run /council — plan a task, or open the Council web UI."""
        parts = text.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        rest = parts[2].strip() if len(parts) > 2 else ""

        # Preserved: a bare /council (and /council stop) is the web UI this
        # command has always opened.
        if sub in ("", "stop"):
            await self._run_chat(text)
            return
        if sub in ("web", "open"):
            await self._run_chat("/council")
            return
        if sub in ("view", "approve", "revise", "reject", "cancel", "history"):
            await self._council_subcommand(sub, rest)
            return

        # Anything else is the task to plan.
        self.run_worker(self._council_plan(text.split(maxsplit=1)[1].strip()))

    async def _council_subcommand(self, sub: str, rest: str) -> None:
        from novacode_cli import council_planner as cp

        store = self._council_store()

        if sub == "history":
            ids = store.history()
            if not ids:
                self._log(Text("No council runs yet.", style="dim"))
                return
            for run_id in ids:
                run = store.load(run_id)
                if run is None:
                    continue
                self._log(
                    Text.assemble(
                        (f"{run_id}  ", "cyan"),
                        (f"{run.status:<18}", "dim"),
                        (run.prompt.splitlines()[0][:60] if run.prompt else "", ""),
                    )
                )
            return

        run = self._council_target(store)
        if run is None:
            self._log(Text("No council run to act on. Try /council <task>.", "yellow"))
            return

        if sub == "view":
            if rest.isdigit():
                await self._council_show_plan(run, int(rest))
            else:
                self._council_render_plans(run)
            return

        if sub in ("reject", "cancel"):
            (cp.reject if sub == "reject" else cp.cancel)(run, store=store)
            self._log(Text(f"Council run {run.id} {run.status}.", style="yellow"))
            return

        if sub == "revise":
            if not rest:
                self._log(Text("Say what to change: /council revise <notes>", "yellow"))
                return
            cp.request_revision(run, rest, store=store)
            self._log(Text("↻ Replanning with your revisions…", style="cyan"))
            self.run_worker(self._council_plan(run.prompt, run=run))
            return

        # approve
        if not rest.split(" ")[0].isdigit():
            self._log(Text("Which plan? /council approve 1", style="yellow"))
            return
        try:
            plan = cp.approve(run, int(rest.split(" ")[0]), store=store)
        except cp.CouncilApprovalError as exc:
            self._log(Text(f"✖ {exc}", style="red"))
            return
        self._log(Text(f"● Plan approved — {run.approved_proposal_id}", "bold green"))
        self._log(Text(f"  saved to .nova/council/{run.id}/approved-plan.md", style="dim"))
        self._log(Text("Handing the approved plan to the coding agent…", "cyan"))
        # The executor is told the plan carries the user's authorization, so it
        # implements rather than re-opening the choice the council already made.
        cp.mark_executing(run, store=store)
        try:
            await self._stream_prompt(
                "The user reviewed and APPROVED the following implementation plan "
                "from a planning council. Implement it. Do not redesign it or "
                "propose alternatives; if a step turns out to be wrong, say so and "
                "stop rather than silently substituting your own approach.\n\n" + plan
            )
        finally:
            # Even on an error, the run must not be left reading "executing"
            # forever — /council history would then misreport it as in flight.
            cp.mark_completed(run, store=store)

    async def _council_plan(self, prompt: str, run: Any = None) -> None:
        """Drive a planning council, logging each phase as it lands."""
        from novacode_cli import council_planner as cp
        from novacode_cli.council import get_council_model

        try:
            model = get_council_model()
        except Exception as exc:  # noqa: BLE001 — no provider configured
            self._log(Text(f"Council unavailable: {exc}", style="red"))
            return

        store = self._council_store()
        from novacode_cli.council_planner import PLANNING_PERSONAS

        self._log(Text("◈ Council convening — planning, not editing.", "bold cyan"))
        # Said up front because this is genuinely slow: four phases of whole
        # structured plans, and a hosted reasoning model can take minutes per
        # call. Without this the user reads a quiet screen as a hang.
        self._log(
            Text(
                f"  {len(PLANNING_PERSONAS)} agents · brainstorm → critique → vote "
                "→ judge. This takes a few minutes.",
                style="dim",
            )
        )
        phase_label = {
            cp.BRAINSTORMING: "Brainstorming independently",
            cp.CRITIQUING: "Critiquing (anonymized)",
            cp.VOTING: "Anonymous ranked vote",
            cp.JUDGING: "Judge scoring against the rubric",
        }
        # Read the project context off the loop: `_council_context` does sync
        # filesystem work (cwd lookup, is_file, read_text) and this runs while the
        # user is watching the transcript, so it would stall the UI.
        council_context = await asyncio.to_thread(self._council_context)
        try:
            async for event in cp.run_planning_council(
                prompt,
                model,
                context=council_context,
                store=store,
                run=run,
            ):
                self._council_run_id = event["run_id"]
                name, payload = event["event"], event["payload"]
                if name == "council.phase.started":
                    label = phase_label.get(payload.get("phase", ""))
                    if label:
                        self._log(Text(f"  → {label}", style="cyan"))
                elif name == "proposal.created":
                    self._log(Text(f"    ● {payload['proposal_id']} — {payload['title']}", "dim"))
                elif name == "agent.failed":
                    # With the reason: "dropped out" alone leaves the user
                    # unable to tell a slow model from an unreachable one.
                    why = payload.get("reason") or "no reply"
                    self._log(Text(f"    ✖ {payload['name']} — {why}", style="yellow"))
                elif name == "vote.tallied":
                    entropy = payload["tally"].get("entropy", 0.0)
                    self._log(Text(f"    ballots in · disagreement {entropy:.2f}", "dim"))
                elif name == "council.failed":
                    self._log(Text(f"✖ {payload['reason']}", style="red"))
                elif name == "plans.selected":
                    latest = store.load(event["run_id"])
                    if latest is not None:
                        self._council_render_plans(latest)
        except Exception as exc:  # noqa: BLE001 — a council must not kill the TUI
            self._log(Text(f"Council failed: {exc}", style="red"))

    def _council_context(self) -> str:
        """Light repo context for the planners: the project's own NOVA.md.

        Deliberately not a retrieval engine — the council plans an approach, and
        the coding agent reads the actual files when it implements. Whatever
        NOVA.md says about the project is the highest-value context per token.
        """
        try:
            for name in ("NOVA.md", ".nova/NOVA.md"):
                path = Path.cwd() / name
                if path.is_file():
                    return path.read_text(encoding="utf-8")[:4000]
        except OSError:
            pass
        return ""

    def _council_render_plans(self, run: Any) -> None:
        """The top-3 cards, plus how to act on them."""
        plans = run.ranked_plans()
        if not plans:
            self._log(Text("No plans were selected.", style="yellow"))
            return
        self._log(Text(f"◈ Council results — {len(plans)} plans", style="bold cyan"))
        if run.judgment is not None and run.judgment.fell_back_to_vote:
            self._log(
                Text(
                    "  ⚠ The judge was unreachable — this order is the council's "
                    "vote, not a rubric verdict.",
                    style="yellow",
                )
            )
        elif run.tally.get("entropy", 0.0) >= 0.9:
            self._log(
                Text(
                    "  ⚠ The council did not converge — this ranking is a "
                    "judgement call, not a consensus.",
                    style="yellow",
                )
            )
        for plan in plans:
            proposal = run.proposal(plan.proposal_id)
            if proposal is None:
                continue
            head = "Recommended" if plan.rank == 1 else "Alternative"
            self._log(
                Text.assemble(
                    (f"  #{plan.rank} ", "bold"),
                    (f"{head} ", "green" if plan.rank == 1 else "dim"),
                    (proposal.title, "bold"),
                )
            )
            self._log(Text(f"     {proposal.summary}", style="dim"))
            for pro in proposal.tradeoffs.get("pros", [])[:2]:
                self._log(Text(f"     + {pro}", style="green"))
            for con in proposal.tradeoffs.get("cons", [])[:2]:
                self._log(Text(f"     - {con}", style="yellow"))
        self._log(
            Text(
                "  /council view <n> · /council approve <n> · /council revise <notes>",
                style="dim",
            )
        )

    async def _council_show_plan(self, run: Any, index: int) -> None:
        plans = run.ranked_plans()
        if not 1 <= index <= len(plans):
            self._log(Text(f"Pick a plan between 1 and {len(plans)}.", "yellow"))
            return
        from novacode_cli.council_planner import to_implementation_plan

        selected = plans[index - 1]
        proposal = run.proposal(selected.proposal_id)
        if proposal is None:
            self._log(Text("That plan is missing from the run.", style="red"))
            return
        self._log(Markdown(to_implementation_plan(run, proposal, selected)))

    async def _run_chat(self, text: str) -> None:
        """Run the Council web UI — start or stop it in the browser."""
        from novacode_cli.commands.chat_handler import (
            get_server_url,
            is_server_running,
            set_agent_refs,
            start_chat_server,
            stop_chat_server,
        )

        parts = text.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""

        # Wire agent refs (same as the CLI handler does)
        set_agent_refs(
            self.agent,
            self.assistant_id,
            self.session_state,
            asyncio.get_running_loop(),
        )

        if sub == "stop":
            if not is_server_running():
                self._log(Text("Council server is not running.", style="yellow"))
                return
            stop_chat_server()
            self._log(Text("● Council server stopped.", style="green"))
            return

        if is_server_running():
            url = get_server_url()
            self._log(Text(f"Council UI already running at {url}", style="green"))
            return

        url = start_chat_server()
        self._log(
            Text(
                f"Council UI started at {url} — present a topic to convene",
                style="bold green",
            )
        )

    async def _run_agents(self) -> None:
        """Show configured subagents (interactive)."""
        await self.push_screen_wait(AgentsScreen())

    async def _run_skills(self) -> None:
        """Show installed skills (interactive)."""
        selected = await self.push_screen_wait(SkillsScreen())
        if selected:
            composer = self.query_one("#prompt", PromptInput)
            composer.text = f"${selected} " + composer.text
            composer.focus()

    def _collect_skill_names(self) -> list[str]:
        from pathlib import Path

        from novacode_cli.config.config import Settings, settings

        dirs: list = []
        try:
            dirs.append(settings.ensure_user_skills_dir())
            dirs.append(settings.get_shared_skills_dir())
        except Exception:  # noqa: BLE001
            pass
        try:
            claude_skills_dir = Settings.get_global_claude_skills_dir()
            if claude_skills_dir.exists():
                dirs.append(claude_skills_dir)
        except Exception:  # noqa: BLE001
            pass
        try:
            dirs.extend(settings.get_project_skills_dirs())
        except Exception:  # noqa: BLE001
            pass
        try:
            from novacode_cli.plugins.claude_plugins import plugin_skill_dirs

            dirs.extend(d for _, d in plugin_skill_dirs())
        except Exception:  # noqa: BLE001
            pass

        names: list[str] = []
        seen: set[str] = set()
        for d in dirs:
            if not d:
                continue
            p = Path(d)
            if not p.exists():
                continue
            for sk in sorted(p.iterdir()):
                if sk.is_dir() and (sk / "SKILL.md").exists() and sk.name not in seen:
                    seen.add(sk.name)
                    names.append(sk.name)
        return names

    def _get_skill_names(self) -> list[str]:
        if self._skill_names_cache is None:
            self._skill_names_cache = self._collect_skill_names()
        return self._skill_names_cache

    def _cached_enabled_skill_count(self) -> int:
        """Number of *enabled* skills (installed minus curation-disabled).

        This is what the status bar shows, so it must drop when skills are
        deactivated via ``/skills``. Resolving the disabled set reads two small
        prefs files, so the result is cached ~1s to keep the throttled status
        tail cheap; ``action_toggle`` sets ``_skill_count_cache = None`` to make
        a toggle reflect immediately.
        """
        now = time.monotonic()
        fresh = self._skill_count_cache is not None and now - self._skill_count_ts < 5.0
        if not fresh and not getattr(self, "_skill_count_busy", False):
            # Computed on a worker thread, never here: this is the status-line
            # path, and the count needs two prefs files plus (first time) a
            # stat of every skill directory. On a busy disk that read blocked
            # the UI loop for up to 30 s. The status line shows the previous
            # value until the new one lands.
            self._skill_count_busy = True
            self._skill_count_ts = now

            def _compute() -> None:
                try:
                    from novacode_cli.skills.skills_prefs import effective_disabled

                    disabled = effective_disabled()
                    self._skill_count_last = sum(
                        1 for n in self._get_skill_names() if n not in disabled
                    )
                    self._skill_count_cache = self._skill_count_last
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    self._skill_count_busy = False

            threading.Thread(target=_compute, name="nova-skill-count", daemon=True).start()
        if self._skill_count_cache is not None:
            return self._skill_count_cache
        return getattr(self, "_skill_count_last", 0)

    def _cached_agent_md_count(self) -> int:
        """Project NOVA.md/CLAUDE.md count, stat'd at most ~once per second.

        ``_refresh_status`` runs at 20fps while a turn is active. Calling
        ``get_project_agent_md_paths()`` there re-stat'd four candidate paths
        every frame (~80 disk stats/sec) — the dominant in-turn UI lag. These
        files don't change mid-frame, so a 1s TTL cache is plenty.
        """
        now = time.monotonic()
        if hasattr(self, "_md_count_cache") and now - self._md_count_ts < 1.0:
            return self._md_count_cache
        try:
            from novacode_cli.config.config import settings

            count = len(settings.get_project_agent_md_paths())
        except Exception:  # noqa: BLE001
            count = 0
        self._md_count_cache = count
        self._md_count_ts = now
        return count

    def _get_agent_names(self) -> list[str]:
        if self._agent_names_cache is None:
            names: list[str] = []
            try:
                from novacode_cli.config.config import settings

                for name, _d, _scope in settings.get_all_agents():
                    names.append(name)
            except Exception:  # noqa: BLE001
                pass
            self._agent_names_cache = names
        return self._agent_names_cache

    # -- slash helpers --------------------------------------------------------
    def _help_text(self) -> Text:
        """Render /help — derived from the TUI_COMMANDS table, so a command
        registered there can never be missing here."""
        from novacode_cli.tui.reference_screens import help_entries

        t = Text()
        entries = help_entries(TUI_COMMANDS, getattr(self, "_plugin_commands", ()))
        for category in dict.fromkeys(entry.category for entry in entries):
            t.append(f"\n{category}\n", style="bold")
            for entry in entries:
                if entry.category == category:
                    t.append(f"  {entry.command}", style="bold")
                    if entry.aliases:
                        t.append(" (" + ", ".join(entry.aliases) + ")")
                    t.append(f" — {entry.description}\n")
        return t

    def _token_text(self) -> Text:
        if self.token_tracker is None:
            return Text("No token data available.", style="dim")
        try:
            bd = self.token_tracker.get_breakdown()
        except Exception:  # noqa: BLE001
            bd = None
        if not bd:
            return Text("No token usage captured yet.", style="dim")
        return Text(
            f"Context: {bd.usage_percentage:.1f}% used ({bd.total_tokens:,} tokens)",
            style="dim",
        )

    async def _render(self, e: Any) -> None:
        if harness := getattr(self, "_ui_harness", None):
            harness.observe(e)
        if isinstance(e, ev.StatusUpdate):
            self._set_status(e.message or "ready")
        elif isinstance(e, ev.ReasoningDelta):
            # Stream the model's reasoning into a dim, transient message widget.
            # The repaint is coalesced to 10fps via _schedule_stream_flush.
            self._reasoning_buf_parts.append(e.text)
            if self._reason_msg is None:
                self._reason_msg = ChatMessage(
                    Text("💭 musing…", style="dim italic"), "reason", collapsible=True
                )
                await self._mount(self._reason_msg)
                # Leading edge: show the first fragment now rather than waiting
                # out the coalescing interval.
                self._paint_stream()
            self._schedule_stream_flush()
            thinking_line = status_phrases.status_line("thinking", sticky_phrase=True)
            if self._activity != thinking_line:
                self._set_status(thinking_line)
        elif isinstance(e, ev.TextDelta):
            # Stream incremental prose into the in-progress Nova message widget.
            # Coalesced repaint (10fps) — see _schedule_stream_flush/_paint_stream.
            self._live_buf_parts.append(e.text)
            if self._stream_msg is None:
                name, color = self._current_agent_info()
                self._stream_msg = ChatMessage(self._agent_label(name, color), "nova")
                await self._mount(self._stream_msg)
                # Leading edge: show the first fragment now rather than waiting
                # out the coalescing interval.
                self._paint_stream()
            self._schedule_stream_flush()
            responding_line = status_phrases.status_line("responding", sticky_phrase=True)
            if self._activity != responding_line:
                self._set_status(responding_line)
        elif isinstance(e, ev.TextDiscard):
            self._stream_flush_scheduled = False
            if self._stream_msg is not None:
                try:
                    await self._stream_msg.remove()
                except Exception:  # noqa: BLE001
                    pass
                self._stream_msg = None
            self._live_buf = ""
        elif isinstance(e, ev.AssistantMessage):
            # Commit: finalize the streaming widget as rendered markdown. Cancel
            # any pending coalesced flush so it can't repaint a finalized widget.
            self._stream_flush_scheduled = False
            if self._stream_msg is not None:
                self._stream_msg.update_header(self._agent_label(e.agent_name, e.agent_color))
                self._stream_msg.update_body(Markdown(e.text))
                self._stream_msg = None
            else:
                await self._add_message(
                    self._agent_label(e.agent_name, e.agent_color),
                    "nova",
                    Markdown(e.text),
                )
            self._live_buf = ""
            await self._remove_reasoning()
            self._scroll_end()
            # Accumulate the reply's prose instead of speaking immediately.
            # Speech is deferred until the turn finishes (ev.Done) or pauses (ev.InterruptRequest)
            # to prevent it from getting cut off by intermediate events or subsequent steps.
            if self._voice_pipeline is not None and self._voice_speak_responses:
                # Speech needs a short summary, not another unbounded copy of
                # every intermediate answer during a long autonomous turn.
                self._accumulated_reply = (self._accumulated_reply + "\n\n" + e.text[-16_000:])[
                    -16_000:
                ]
            else:
                self._accumulated_reply = ""
            self._schedule_prune()
        elif isinstance(e, ev.ToolCall):
            self._set_status(f"running {e.name}…")
            base = f"{e.icon} {_esc(e.display_str)}"
            if e.name in _DETAILED_TOOL_NAMES:
                # Write/edit and execution tools keep a DEDICATED Collapsible.
                # Mounting it (via _mount) closes any open tool group, keeping
                # transcript order correct.
                if e.name in {
                    "shell",
                    "bash",
                    "execute",
                    "execute_bash",
                    "run_command",
                    "run_tests",
                    "start_dev_server",
                }:
                    body = OutputLog(
                        classes="terminal-log",
                        highlight=True,
                        markup=True,
                        max_lines=_LOG_MAX_LINES,
                    )
                    # Starts expanded (collapsed=False) to show live output!
                    hint = Static(
                        Text("Ctrl+B · run in background", style="dim"),
                        classes="background-shell-hint",
                    )
                    comp = Collapsible(body, hint, title=f"{base}  · running…", collapsed=False)
                else:
                    body = Static("", classes="toolbody")
                    # Starts collapsed for file diffs
                    comp = Collapsible(body, title=f"{base}  · running…", collapsed=True)
                comp.add_class("tool")
                comp.add_class("tool-active")
                animate_entrance(comp, "zoom")
                await self._mount(comp)
                entry = (comp, body, base)
                if e.call_id:
                    self._tool_components[e.call_id] = entry
                self._last_tool = entry
            else:
                # Everything else (reads, search, exec, MCP, …) condenses into
                # the shared tool group — one compact line per call.
                await self._ensure_tool_group()
                self._add_tool_group_call(e.call_id, f"{e.icon} {e.display_str}", e.name)
        elif isinstance(e, ev.ToolResult):
            if e.call_id and e.call_id in self._tool_components:
                # Dedicated panel (write/edit) — finalize with full output body.
                self._finalize_tool(e.call_id, e.preview, e.full_output, is_error=e.is_error)
            else:
                self._mark_tool_group_result(e.call_id, is_error=e.is_error, detail=e.preview)
                if e.is_error:
                    # Keep the untruncated output so the group can expand it
                    # (see _set_tool_error_detail). Must run AFTER the mark,
                    # which is what registers the entry's index.
                    self._set_tool_error_detail(e.call_id, e.full_output)
                if e.call_id is not None:
                    self._tool_group_lines.pop(e.call_id, None)
                self._trim_tool_group_history()
            self._scroll_end()
            self._schedule_prune()
        elif isinstance(e, ev.FileOp):
            # File ops are the result of their tool call. Write/edit (a dedicated
            # panel was opened at ToolCall) render the full colored diff body so
            # the user can see exactly what changed; reads condense into the
            # group with a concise "Read N lines" summary.
            rec = e.record
            errored = bool(getattr(rec, "error", None)) or (getattr(rec, "status", "") == "error")
            if e.call_id and e.call_id in self._tool_components:
                comp, body, base = self._tool_components.pop(e.call_id)
                if self._last_tool is not None and self._last_tool[0] is comp:
                    self._last_tool = None
                # A bullet, not ✓/✗: the emoji are painted by the terminal's own
                # font, so /theme could not move them (see _render_tool_line).
                mark = "•"
                comp.title = f"{base}  {mark} {_esc(self._fileop_summary(rec))}".rstrip()
                # Expand on failure (surface the error) AND on a successful change
                # with a diff — these dedicated write/edit panels exist precisely
                # to show what changed, so a collapsed diff defeats the purpose.
                if errored or getattr(rec, "diff", None):
                    comp.collapsed = False
                body.update(self._fileop_body(rec, e.full_output))
            else:
                self._mark_tool_group_result(
                    e.call_id, is_error=errored, detail=self._fileop_summary(rec)
                )
                if e.call_id is not None:
                    self._tool_group_lines.pop(e.call_id, None)
                self._trim_tool_group_history()
            self._scroll_end()
            self._schedule_prune()
        elif isinstance(e, ev.TodoUpdate):
            # Held in per-pane state so a session switch can repaint the
            # app-global dock with the pane the user is actually looking at.
            self._todos = list(e.todos or [])
            self._todos_agent = e.agent_name
            self._paint_todos(self._todos, e.agent_name)
        elif isinstance(e, ev.ErrorOutput):
            lines = e.text.splitlines()
            summary = next(
                (line.strip() for line in reversed(lines) if line.strip()), "Command failed"
            )
            title = f"Error output · {self._oneline(summary)} · {len(lines)} lines"
            output = OutputLog(classes="terminal-log", max_lines=_LOG_MAX_LINES, markup=False)
            output.write(Text(e.text, style=self._palette.tool_fail))
            card = Collapsible(output, title=_esc(title), collapsed=True)
            card.add_class("tool", "error-output")
            card.styles.border_left = ("thick", self._palette.tool_fail)
            self._close_tool_group()
            await self._mount(card)
            self._scroll_end(force=False)
        elif isinstance(e, ev.CompactionNotice):
            self._stop_compaction_indicator()
            self._log(Text("⟳ Context compacted", style="dim"))
            # Context just shrank. The API-sourced current_context is the turn's
            # PEAK (pre-compaction) and would otherwise mask the reduction, so
            # reset() clears has_api_data and lets the recomputed, message-based
            # breakdown show the real post-compaction size. Then refresh the
            # status line so ctx% reflects it immediately.
            if self.token_tracker is not None:
                try:
                    self.token_tracker.reset()
                    await self._update_context_breakdown()
                except Exception:  # noqa: BLE001
                    pass
                self._refresh_status()
        elif isinstance(e, ev.ContextMessage):
            if e.event_type == "nova_compaction_start":
                self._start_compaction_indicator()
                return
            # Review-cycle start/complete are transient status, not log entries:
            # surface them on the live indicator above the input instead of
            # letting them scroll away in the transcript.
            if e.event_type == "nova_review_start":
                self._set_nova_indicator(f"{e.icon} {e.message}", style=self._event_color(e.color))
                return
            if e.event_type == "nova_review_complete":
                # Show briefly, then fade so the indicator doesn't linger.
                self._set_nova_indicator(
                    f"{e.icon} {e.message}", style=self._event_color(e.color), auto_clear=4.0
                )
                return

            color = self._event_color(e.color)
            t = Text(e.icon + " ", style=color)
            t.append(e.message, style=color)
            # Map event_type (e.g. "nova_skill_refinement") to the CSS modifier
            # class ("nova-skill-refinement"): strip the leading "nova_"
            # namespace and convert underscores to hyphens so the per-event
            # border colors defined in the stylesheet actually match.
            css = "nova-event"
            if e.event_type:
                modifier = e.event_type.replace("nova_", "", 1).replace("_", "-")
                css += f" nova-{modifier}"
            # Non-tool content: close any open tool group first to keep order.
            self._close_tool_group()
            self._transcript().mount(Static(t, classes=css))
            self._schedule_prune()
            self._scroll_end()
        elif isinstance(e, ev.SubagentActivity):
            await self._handle_subagent(e)
        elif isinstance(e, ev.SubagentTask):
            # The panel's own row for the same dispatch: a fan-out is visible as
            # a list while it runs, rather than as N cards.
            self._ingest_subagent_task(e)
        elif isinstance(e, ev.SubagentPreview):
            # Collected unseen; the preview screen reads it when a row is opened.
            subagent_tasks.append_preview(
                self._subagent_previews.setdefault(e.task_id, []), e.kind, e.text
            )
        elif isinstance(e, ev.UsageUpdate):
            if self.token_tracker is not None:
                try:
                    self.token_tracker.add(
                        e.input_tokens,
                        e.output_tokens,
                        cache_read_tokens=e.cache_read_tokens,
                        cache_creation_tokens=e.cache_creation_tokens,
                        session_tokens=e.session_tokens,
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif isinstance(e, ev.InterruptRequest):
            if getattr(self, "_accumulated_reply", None):
                self._speak_reply(self._accumulated_reply)
                self._accumulated_reply = ""
            await self._handle_interrupt(e)
        elif isinstance(e, ev.Cancelled):
            self._stop_compaction_indicator()
            self._accumulated_reply = ""
            self._stop_foreground_subagents()
            if getattr(self, "_detach_cancelling", False):
                # The turn was cancelled by a Ctrl+B detach, not a real interrupt —
                # the command is now running as a background task.
                self._log(Text("● Command moved to background — agent is idle.", style="cyan"))
            else:
                self._log(Text("Interrupted.", style="yellow"))
        elif isinstance(e, ev.ContextOverflow):
            self._stop_compaction_indicator()
            # The provider rejected the request for being too long. Unlike other
            # provider errors this has one specific remedy — shrink the
            # conversation — so compact and retry ONCE. A second overflow after
            # compacting means the summary itself doesn't fit, and retrying
            # again would just loop.
            self._accumulated_reply = ""
            if getattr(self, "_overflow_retried", False):
                self._overflow_retried = False
                self._log(
                    Text(
                        "⚠ Still over the context limit after compacting — the "
                        "conversation can't be shrunk further. Use /clear to start "
                        "fresh, or switch to a larger-context model.",
                        style="bold #f7768e",
                    )
                )
                for line in (e.message or "").splitlines():
                    self._log(Text(line, style="yellow"))
                return
            self._overflow_retried = True
            self._log(Text("⚠ Context overflow — compacting and retrying…", style="bold #f7768e"))
            await self._run_compact("")
            prompt = getattr(self, "_last_user_prompt", None)
            if prompt:
                await self._stream_prompt(prompt)
            else:
                self._log(
                    Text(
                        "Compacted. Re-send your message to continue.",
                        style="dim",
                    )
                )
            self._overflow_retried = False
        elif isinstance(e, ev.Error):
            self._stop_compaction_indicator()
            self._accumulated_reply = ""
            self._stop_foreground_subagents()
            # Provider failures (usage/rate limit, auth, connectivity) are
            # pre-formatted into a clean notice upstream and flagged; render them
            # as a calm warning. The formatter fallback covers any Error that
            # carries a raw provider exception without the flag.
            notice = e.message if e.is_provider_notice else None
            if notice is None and e.exception is not None:
                from novacode_cli.errors import friendly_model_error

                notice = friendly_model_error(e.exception)
            if notice:
                for line in notice.splitlines():
                    self._log(Text(line, style="yellow"))
            else:
                self._log(Text(f"Error: {e.message}", style="red"))
        elif isinstance(e, ev.Done):
            self._stop_compaction_indicator()
            self._stop_foreground_subagents()
            if getattr(self, "_accumulated_reply", None):
                self._speak_reply(self._accumulated_reply)
                self._accumulated_reply = ""
            await self._sync_async_task_watcher()
            # The turn is over, so the panel's rows are final: leave them readable
            # (folded to the header) until the next turn's first dispatch.
            self._subagents_stale = True
            if self._subagent_rows:
                self._subagents_collapsed = True
                self._paint_subagents()

    def _notify_async_done(self, level: str, title: str, message: str) -> None:
        """Surface a finished async subagent as a notification, and — when the
        agent is idle — proactively trigger a turn that fetches and reports the
        result, so the user doesn't have to ask for a status report."""
        root = getattr(self, "_root_pane", None)
        hidden = root is not None and root is not getattr(self, "_active_pane", None)
        state = root.state.get("session_state") if hidden else self.session_state
        try:
            state.add_notification(level, title, message, source="async-agent")
        except Exception:  # noqa: BLE001
            pass
        # Extract the full task_id the watcher embeds so we can drive a turn.
        m = re.search(r"\[async_task_id=([^\]]+)\]", message)
        task_id = m.group(1) if m else None
        if not task_id:
            return
        if hidden:
            self._queue_pending_job_note(
                f"Async task {task_id} finished. Fetch it with check_async_task('{task_id}')."
            )
            return
        # Only auto-report when the agent is idle — never interrupt an active
        # turn. If busy, the notification (🔔 badge) still surfaces the event.
        if getattr(self, "_turn_active", False):
            return
        self._log(Text(f"↻ Async agent finished — fetching result…", style="cyan"))
        self._auto_report_async_done(task_id)

    @work(exclusive=True, group="turn")
    async def _auto_report_async_done(self, task_id: str) -> None:
        """Proactively report a finished async subagent's result.

        Injects a visible message into the transcript and runs a turn that asks
        the agent to fetch the task result and summarize it — closing the gap
        where a finished remote agent otherwise waits for the user to ask."""
        if getattr(getattr(self, "_active_pane", None), "kind", "root") != "root":
            self._queue_pending_job_note(
                f"Async task {task_id} finished. Fetch it with check_async_task('{task_id}')."
            )
            return
        try:
            await self._add_message(
                self._user_label(),
                "user",
                Text(f"[async agent finished — auto-reporting result]"),
            )
        except Exception:  # noqa: BLE001
            pass
        prompt = (
            f"[Async subagent finished] A remote async subagent has completed. "
            f"Fetch its result with check_async_task('{task_id}') and give the "
            f"user a concise summary of what it produced. If the task errored, "
            f"report the error clearly."
        )
        await self._stream_prompt(prompt)
        await self._maybe_run_approved_plan()

    async def _sync_async_task_watcher(self) -> None:
        """After a turn, watch any newly-launched async subagent so its completion
        surfaces as a notification. Async subagents are otherwise fire-and-forget
        (start returns a task_id and nothing pushes completion back), so without
        this a finished remote agent never reaches the user until they poll."""
        if self.agent is None or self.session_state is None:
            return
        try:
            config = {"configurable": {"thread_id": self.session_state.thread_id}}
            state = await asyncio.wait_for(self.agent.aget_state(config), timeout=5.0)
            tasks = state.values.get("async_tasks") or {}
        except Exception:  # noqa: BLE001 — never let telemetry break a turn
            return
        if not tasks:
            return
        watcher = getattr(self, "_async_watcher", None)
        if watcher is None:
            from novacode_cli.remote.async_task_watcher import AsyncTaskWatcher

            watcher = AsyncTaskWatcher(self._notify_async_done)
            self._async_watcher = watcher
        watcher.sync_from_state(tasks)
        self._ingest_async_tasks(tasks)
        # Surface newly-running agents in the ⚙ tasks bar right away (and start
        # its 1s runtime ticker, which will also drop them once they finish).
        try:
            self._refresh_tasks_bar()
        except Exception:  # noqa: BLE001
            pass

    async def _ask_remote_question(self, question_request: dict) -> dict:
        """Route an agent question to the remote user via Discord/Telegram."""
        prompt = (
            question_request.get("question")
            or question_request.get("prompt")
            or "The agent has a question:"
        )
        opts = question_request.get("options") or []
        context = question_request.get("context")

        from novacode_cli.ui.question_prompt import QuestionResponse

        # Real markdown: the bridge renders it. Single asterisks are *italic*
        # there, so the old "*Question:*" labels came out italic, not bold.
        lines = []
        if context:
            lines.append(f"ℹ️ **Context:** {context}\n")
        lines.append(f"❓ **Question:** {prompt}")
        if opts:
            lines.append("\n**Options:**")
            for i, opt in enumerate(opts, 1):
                lines.append(f"{i}. {opt}")
            lines.append("\n_Reply with the number or the option text._")
        message_text = "\n".join(lines)

        retract = None
        try:
            ask_fn = getattr(self._remote_msg, "ask_fn", None)
            if ask_fn is not None:
                retract = await ask_fn(message_text)
            else:
                await self._remote_msg.reply_fn(message_text)
        except Exception as ex:  # noqa: BLE001
            # The user never saw the question, so waiting for their answer would
            # hang the turn forever. Let the agent continue without one.
            self._log(Text(f"Failed to send remote question: {ex}", style="red"))
            return {"response": QuestionResponse(answer="", selected_index=None)}

        self._remote_question_future = asyncio.Future()
        try:
            m = await self._remote_question_future
        finally:
            self._remote_question_future = None
            # Answered (or the turn was cancelled): take the question down, so
            # the chat never shows a question that is no longer open.
            if retract is not None:
                with contextlib.suppress(Exception):
                    await retract()

        text = (getattr(m, "text", "") or "").strip()
        selected = None
        answer = text
        if text.isdigit() and opts:
            idx = int(text) - 1
            if 0 <= idx < len(opts):
                selected = idx
                answer = opts[idx]
        elif opts:
            for i, opt in enumerate(opts):
                if opt.lower() == text.lower():
                    selected = i
                    answer = opt
                    break

        return {"response": QuestionResponse(answer=answer, selected_index=selected)}

    @on(QuestionDock.Answered)
    def _on_question_answered(self, message: QuestionDock.Answered) -> None:
        """Resolve the pending question with the option the user chose."""
        from novacode_cli.ui.question_prompt import QuestionResponse

        self._resolve_question(
            {
                "response": QuestionResponse(
                    answer=message.answer, selected_index=message.selected_index
                )
            }
        )

    @on(QuestionDock.Dismissed)
    def _on_question_dismissed(self, _message: QuestionDock.Dismissed) -> None:
        """Escape dismisses the question only — the turn keeps running."""
        from novacode_cli.ui.question_prompt import QuestionResponse

        self._resolve_question({"response": QuestionResponse(answer="", selected_index=None)})

    def _resolve_question(self, result: dict) -> None:
        future = self._question_future
        if future is not None and not future.done():
            future.set_result(result)

    async def _show_question_dock(self, payload: object) -> dict:
        """Show the docked answer list and await the user's answer.

        The dock is already mounted inside ``#prompt-dock``; this fills it,
        reveals it, and waits for the ``Answered``/``Dismissed`` message the
        widget posts. A docked widget is not a screen, so there is no
        ``push_screen_wait`` to block on — hence the explicit future.

        Returns:
            The same ``{"response": QuestionResponse}`` shape ``QuestionModal``
            produced, so the tool and the remote bridge are unaffected.
        """
        dock = self.query_one("#question-dock", QuestionDock)
        self._question_future = asyncio.get_running_loop().create_future()
        dock.activate(payload if isinstance(payload, dict) else {})
        try:
            return await self._question_future
        finally:
            self._question_future = None
            dock.deactivate()
            # Hand focus back, or the prompt stays dead to the keyboard.
            self.query_one("#prompt", PromptInput).focus()

    async def _handle_interrupt(self, e: "ev.InterruptRequest") -> None:
        # Resolve in a finally so a handler that raises before set_result fails
        # closed (reject) instead of leaving the agent loop awaiting forever.
        try:
            await self._handle_interrupt_inner(e)
        finally:
            if not e.future.done():
                from novacode_cli.core.agent_loop import default_interrupt_response

                e.future.set_result(default_interrupt_response(e.kind))

    async def _handle_interrupt_inner(self, e: "ev.InterruptRequest") -> None:
        if e.kind == "tool":
            req = e.payload
            from novacode_cli.ui.hitl_approval import check_plan_mode_blocked

            blocked, rejection = check_plan_mode_blocked(req, self.session_state.plan_mode_enabled)
            if blocked and rejection:
                e.future.set_result({"decisions": rejection["decisions"], "any_rejected": True})
                return
            action_requests = req["action_requests"]
            if self.session_state.auto_approve:
                e.future.set_result(
                    {
                        "decisions": [{"type": "approve"} for _ in action_requests],
                        "any_rejected": False,
                    }
                )
                return
            choice = await self.push_screen_wait(
                ApprovalModal(
                    "Tool action requires approval",
                    _approval_details(action_requests),
                )
            )
            if choice not in {"approve", "session", "always", "auto"}:
                e.future.set_result(
                    {
                        "decisions": [
                            {"type": "reject", "message": "Rejected by user"}
                            for _ in action_requests
                        ],
                        "any_rejected": True,
                    }
                )
                return

            if choice in ("session", "always"):
                from dataclasses import replace

                from novacode_cli.security.remember import apply_remember
                from novacode_cli.security.rule_synthesis import synthesize_rule

                for ar in action_requests:
                    name, args = ar.get("name", ""), ar.get("args", {})
                    if choice == "session":
                        apply_remember("session", name, args)
                        self._log(Text(f"● Allowed `{name}` for this session.", style="green"))
                    else:
                        rule = synthesize_rule(name, args)
                        out = await self.push_screen_wait(RememberRuleModal(rule))
                        if out:
                            edited = replace(rule, value=out["value"])
                            res = apply_remember(
                                "always", name, args, target=out["target"], rule=edited
                            )
                            if res.saved_path is not None:
                                self._log(Text(f"● Saved to {res.saved_path}", style="green"))
                            else:
                                self._log(
                                    Text(
                                        f"⚠ Could not save rule ({res.error}); "
                                        "kept for this session.",
                                        style="yellow",
                                    )
                                )
                        else:
                            self._log(Text("Not saved — approved this call only.", style="dim"))
                e.future.set_result(
                    {
                        "decisions": [{"type": "approve"} for _ in action_requests],
                        "any_rejected": False,
                    }
                )
                return

            if choice == "auto":
                # Approve everything for the rest of this session.
                self.session_state.auto_approve = True
                self._log(Text("● Auto-approve enabled for this session.", style="green"))
            e.future.set_result(
                {
                    "decisions": [{"type": "approve"} for _ in action_requests],
                    "any_rejected": False,
                }
            )
        elif e.kind == "question":
            if self._remote_msg is not None:
                result = await self._ask_remote_question(e.payload)
            else:
                result = await self._show_question_dock(e.payload)
            e.future.set_result(result)
        elif e.kind == "plan":
            body: Any = "Review the plan and approve to proceed."
            content = None
            try:
                from novacode_cli.ui.interrupt_handlers import resolve_plan_content

                content, _ = resolve_plan_content(
                    getattr(self.session_state, "todos", None),
                    self.session_state,
                    backend=self.backend,
                    inline_plan=(
                        (e.payload or {}).get("plan") if isinstance(e.payload, dict) else None
                    ),
                )
                if content:
                    body = Markdown(content)
            except Exception:  # noqa: BLE001
                pass
            choice = await self.push_screen_wait(PlanApprovalModal("Plan requires approval", body))
            if choice in ("auto", "manual"):
                self.session_state.plan_mode_enabled = False
                self._update_mode_badge()
                if choice == "auto":
                    # "Auto-approve edits" is scoped to THIS plan's execution
                    # run. auto_approve is session-global and agent_loop
                    # auto-approves FUTURE plan interrupts when it's set — so
                    # without restoring it afterwards, approving one plan with
                    # "auto" silently self-approved every later plan.
                    # _maybe_run_approved_plan restores the flag when the run
                    # ends (unless the user had auto-approve on globally).
                    if not getattr(self.session_state, "auto_approve", False):
                        self._plan_scoped_auto_approve = True
                    self.session_state.auto_approve = True
                # Store the plan for hand-off ONLY when a separate plan agent
                # (/plan) produced it. That agent never executes — agent_loop
                # breaks the turn on approval — so _maybe_run_approved_plan runs
                # the plan on the main agent.
                #
                # For main-agent self-planning (plan_agent is None), the agent
                # RESUMES in-context after approval and executes the plan inline.
                # Stashing it here would make _maybe_run_approved_plan fire a
                # SECOND time — clearing the session and re-executing finished
                # work. This mirrors agent_loop's `using_separate_plan_agent` gate.
                if content and getattr(self.session_state, "plan_agent", None) is not None:
                    try:
                        self.session_state.set_approved_plan(content)
                    except Exception:  # noqa: BLE001
                        pass
                e.future.set_result(
                    {
                        "response": {
                            "approved": True,
                            "mode": choice,
                        },
                        "state_update": {"plan_mode_enabled": False},
                    }
                )
            else:
                # Refine — stay in plan mode; the user's next message routes to
                # the plan agent to revise (the "chat to refine" flow).
                e.future.set_result(
                    {
                        "response": {
                            "approved": False,
                            "action": "refine",
                            "feedback": "",
                        },
                        "state_update": {},
                    }
                )
        else:
            e.future.set_result(None)


async def run_tui(
    *,
    agent,
    assistant_id,
    session_state,
    backend,
    token_tracker,
    image_tracker,
    model_name,
    model_provider: str | None = None,
    session_manager=None,
    restored_messages=None,
    sandbox_id: str | None = None,
    sandbox_type: str | None = None,
    sandbox_meta: dict | None = None,
) -> None:
    """Launch the Textual chat app and run until the user exits."""
    app = NovaApp(
        agent=agent,
        assistant_id=assistant_id,
        session_state=session_state,
        backend=backend,
        token_tracker=token_tracker,
        image_tracker=image_tracker,
        model_name=model_name,
        model_provider=model_provider,
        session_manager=session_manager,
        restored_messages=restored_messages,
        sandbox_id=sandbox_id,
        sandbox_type=sandbox_type,
        sandbox_meta=sandbox_meta,
    )
    try:
        await app.run_async()
    finally:
        await app._save_session(
            pane=getattr(app, "_root_pane", None),
            task_status="crashed" if getattr(app, "_exception", None) else "active",
        )
    if not getattr(app, "_exception", None):
        from novacode_cli.ui.exit_summary import print_exit_summary

        with contextlib.suppress(OSError):
            await print_exit_summary(app)
